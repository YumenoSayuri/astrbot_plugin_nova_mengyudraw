"""
Nova CloudComfyUI - 云端 AI 绘图插件
默认通过 Node undici 桥接请求云端接口，Python 路径仅保留作兼容/兜底逻辑
"""

import asyncio
import base64
import json
import random
import re
import struct
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from PIL import Image as PILImage

import httpx

from astrbot.api.message_components import Image as AstrImageComponent
from astrbot.api.message_components import Reply

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools


class NovaCloudComfyUI(Star):
    """Nova CloudComfyUI - 云端 AI 绘图接口生成图片"""

    RESOLUTION_PRESETS = {
        "square": "512x512",
        "landscape": "768x512",
        "portrait": "512x768",
        "1024": "1024x1024",
    }

    MODEL_CHOICES = {
        0: "Miaomiao Harem vPred Dogma 1.1",
        1: "MiaoMiao Pixel 像素 1.0",
        2: "NoobAIXL V1.1",
        3: "illustrious_pencil 融合",
        4: "[全新模型]one_obsession",
        5: "[全新模型]MiaoMiao RealSkin EPS 1.3",
        6: "[全新模型]Newbie exp 0.1",
        7: "[全新模型]MiaoMiao RealSkin vPred 1.1",
        8: "[新服开放]MiaoMiao RealSkin vPred 1.0",
        9: "[全新模型]Wainsfw illustrious v17",
        10: "[全新模型]Wainsfw illustrious v16",
        11: "[新服开放]Wainsfw illustrious v15",
        12: "[新服开放]MiaoMiao Harem 1.75",
        13: "[新服开放]MiaoMiao Harem 1.6G",
        14: "Qwen Image Edit2511版",
    }

    VIDEO_MODEL_INDEXES = set()
    QUALITY_PROMPT_TAGS = [
        "masterpiece",
        "best quality",
        "high quality",
    ]
    AUTO_EDIT_TARGET_MAX_SIDE = 1980

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.plugin_dir = Path(__file__).resolve().parent
        self.image_dir = Path(self.data_dir) / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)

        self._user_last_request: dict[str, float] = {}
        self._processing_users: set[str] = set()
        self._client: httpx.AsyncClient | None = None
        self._moderation_client: httpx.AsyncClient | None = None
        self._moderation_credential_index = 0
        self._base_url: str = ""
        self._api_key: str = ""
        self._proxy_url: str = ""
        self._request_backend: str = "python"

    def _ensure_node_dependencies(self) -> None:
        package_json = self.plugin_dir / "package.json"
        node_modules_undici = self.plugin_dir / "node_modules" / "undici"

        if node_modules_undici.exists():
            logger.info("[NovaCloudComfyUI] 检测到 undici 依赖已存在")
            return

        if not package_json.exists():
            raise RuntimeError("缺少 package.json，无法自动安装 Node 依赖")

        logger.warning("[NovaCloudComfyUI] 未检测到 undici，开始自动执行 npm install")
        result = subprocess.run(
            ["npm", "install"],
            cwd=str(self.plugin_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            check=False,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"自动安装 Node 依赖失败: {(result.stderr or result.stdout or '未知错误')[:1000]}"
            )

        if not node_modules_undici.exists():
            raise RuntimeError("npm install 执行完成，但未找到 node_modules/undici")

        logger.info("[NovaCloudComfyUI] Node 依赖安装完成")

    async def initialize(self):
        """初始化插件"""
        timeout = httpx.Timeout(self.config.get("timeout", 180))
        proxy_url = str(self.config.get("proxy_url", "") or "").strip()
        request_transport = (
            httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None
        )

        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            transport=request_transport,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        moderation_timeout = max(1, int(self.config.get("moderation_timeout", 10)))
        moderation_transport = (
            httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None
        )
        self._moderation_client = httpx.AsyncClient(
            timeout=httpx.Timeout(moderation_timeout),
            follow_redirects=True,
            transport=moderation_transport,
        )

        self._base_url = self.config.get("base_url", "https://sd.exacg.cc").strip().rstrip("/")
        self._api_key = self.config.get("api_key", "").strip()
        self._proxy_url = proxy_url
        self._request_backend = self.config.get("request_backend", "python")

        if self._request_backend == "node":
            self._ensure_node_dependencies()

        logger.info(f"[NovaCloudComfyUI] API Base URL: {self._base_url}")
        logger.info(f"[NovaCloudComfyUI] Proxy: {self._proxy_url or 'disabled'}")
        logger.info(f"[NovaCloudComfyUI] Request Backend: {self._request_backend}")
        logger.info("[NovaCloudComfyUI] 插件初始化完成")

    async def terminate(self):
        """清理资源"""
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._moderation_client:
            await self._moderation_client.aclose()
            self._moderation_client = None
        logger.info("[NovaCloudComfyUI] 插件已终止")

    def _parse_resolution(self, resolution: str | None) -> tuple[int, int]:
        """解析分辨率字符串，返回 width, height"""
        if not resolution:
            resolution = self.config.get("default_resolution", "512x768")

        res = str(resolution).strip().lower()
        if res in self.RESOLUTION_PRESETS:
            res = self.RESOLUTION_PRESETS[res]

        normalized = (
            res.replace("×", "x")
            .replace("✕", "x")
            .replace("✖", "x")
            .replace("*", "x")
            .replace("X", "x")
            .replace("-", "x")
        )
        match = re.match(r"(\d+)\s*x\s*(\d+)", normalized)
        if match:
            width = max(64, min(2048, int(match.group(1))))
            height = max(64, min(2048, int(match.group(2))))
            return width, height

        fallback = self.config.get("default_resolution", "512x768")
        return self._parse_resolution(fallback if fallback != resolution else "512x768")

    def _check_debounce(self, user_id: str) -> bool:
        """检查防抖，返回 True 表示需要拒绝"""
        interval = self.config.get("debounce_interval", 15)
        if interval <= 0:
            return False

        now = time.time()
        last = self._user_last_request.get(user_id, 0)
        if now - last < interval:
            return True

        self._user_last_request[user_id] = now
        return False

    def _normalize_steps(self, steps: str | int | None, model_index: int) -> int:
        try:
            value = int(steps) if steps not in ("", None) else 0
        except (TypeError, ValueError):
            value = 0

        if value <= 0:
            value = int(self.config.get("default_steps", 20))

        if self._is_edit_model_index(model_index):
            return value

        return max(1, min(50, value))

    def _normalize_cfg(self, cfg: str | float | None, model_index: int) -> float:
        try:
            value = float(cfg) if cfg not in ("", None) else 0.0
        except (TypeError, ValueError):
            value = 0.0

        if value <= 0:
            value = float(self.config.get("default_cfg", 5.0))

        if self._is_edit_model_index(model_index):
            return value

        return max(1.0, min(10.0, value))

    def _normalize_model_index(self, model_index: str | int | None) -> int:
        try:
            value = int(model_index) if model_index not in ("", None) else -1
        except (TypeError, ValueError):
            value = -1

        if value < 0:
            value = int(self.config.get("default_model_index", 10))
        return value

    def _get_default_edit_model_index(self) -> int:
        try:
            value = int(self.config.get("default_edit_model_index", 14))
        except (TypeError, ValueError):
            value = 14

        return value if value >= 0 else 14

    def _get_edit_model_indexes(self) -> set[int]:
        return {self._get_default_edit_model_index()}

    def _is_edit_model_index(self, model_index: int) -> bool:
        return model_index in self._get_edit_model_indexes()

    def _normalize_seed(self, seed: str | int | None, model_index: int) -> int:
        try:
            value = int(seed) if seed not in ("", None) else 0
        except (TypeError, ValueError):
            value = 0

        if self._is_edit_model_index(model_index):
            return value if value > 0 else -1

        if value > 0:
            return value

        return random.randint(1, 2_147_483_647)

    def _build_request_payload(
        self,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        steps: int,
        cfg: float,
        model_index: int,
        seed: int,
        image_source: str = "",
    ) -> dict[str, Any]:
        if model_index in self.VIDEO_MODEL_INDEXES:
            raise RuntimeError("当前插件暂不支持视频模型，请改用图片模型")

        if self._is_edit_model_index(model_index):
            if not image_source:
                raise RuntimeError("图片编辑模型必须提供 image_source 原图地址")
            return {
                "prompt": prompt,
                "model_index": model_index,
                "image_source": image_source,
                "width": width,
                "height": height,
            }

        return {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg": cfg,
            "model_index": model_index,
            "seed": seed,
        }

    def _is_cloudflare_block_response(self, status_code: int, content_type: str, text: str) -> bool:
        lowered = (text or "").lower()
        ctype = (content_type or "").lower()
        return status_code == 403 and (
            "just a moment" in lowered
            or "cloudflare" in lowered
            or "text/html" in ctype
        )

    async def _call_generate_api_httpx(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        url = f"{self._base_url}/api/v1/generate_image"
        logger.info(f"[NovaCloudComfyUI] HTTPX 请求云端接口: {url}")

        try:
            response = await self._client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise RuntimeError("云端接口请求超时，请稍后重试") from exc

        content_type = response.headers.get("content-type", "")
        response_text = response.text

        if self._is_cloudflare_block_response(response.status_code, content_type, response_text):
            raise RuntimeError("CLOUDFLARE_BLOCKED")

        if response.status_code != 200:
            try:
                error_json = response.json()
                error_message = error_json.get("error") or error_json.get("message") or str(error_json)
            except Exception:
                error_message = response_text[:500]
            raise RuntimeError(f"云端接口请求失败 ({response.status_code}): {error_message}")

        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"云端接口响应解析失败: {exc}") from exc

    async def _call_generate_api_undici(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        bridge_path = self.plugin_dir / "undici_bridge.mjs"
        if not bridge_path.exists():
            raise RuntimeError("undici_bridge.mjs 不存在，无法使用 Node 桥接")

        command = [
            "node",
            str(bridge_path),
            "--url",
            f"{self._base_url}/api/v1/generate_image",
            "--method",
            "POST",
            "--api-key",
            self._api_key,
            "--data",
            json.dumps(payload, ensure_ascii=False),
        ]

        if self._proxy_url:
            command.extend(["--proxy", self._proxy_url])

        logger.info("[NovaCloudComfyUI] 使用 undici 桥接请求云端接口")
        result = subprocess.run(
            command,
            cwd=str(self.plugin_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=int(self.config.get("timeout", 180)) + 10,
            check=False,
        )

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            raise RuntimeError(f"undici 桥接执行失败: {stderr or stdout or '未知错误'}")

        try:
            bridge_result = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"undici 桥接响应解析失败: {stdout[:500]}") from exc

        if "error" in bridge_result:
            raise RuntimeError(f"undici 桥接错误: {bridge_result.get('error')}")

        status_code = int(bridge_result.get("status", 0) or 0)
        headers = bridge_result.get("headers") or {}
        body = bridge_result.get("body")
        body_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)

        if self._is_cloudflare_block_response(status_code, headers.get("content-type", ""), body_text):
            raise RuntimeError("undici 桥接后仍被 Cloudflare 拦截")

        if status_code != 200:
            if isinstance(body, dict):
                error_message = body.get("error") or body.get("message") or json.dumps(body, ensure_ascii=False)
            else:
                error_message = body_text[:500]
            raise RuntimeError(f"云端接口请求失败 ({status_code}): {error_message}")

        if not isinstance(body, dict):
            raise RuntimeError("undici 桥接返回的 body 不是 JSON 对象")

        return body

    async def _call_generate_api(
        self,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        steps: int,
        cfg: float,
        model_index: int,
        seed: int,
        image_source: str = "",
    ) -> dict[str, Any]:
        if not self._api_key:
            raise RuntimeError("未配置 api_key，无法调用云端绘图接口")

        payload = self._build_request_payload(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            steps=steps,
            cfg=cfg,
            model_index=model_index,
            seed=seed,
            image_source=image_source,
        )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        logger.debug(f"[NovaCloudComfyUI] 请求体: {json.dumps(payload, ensure_ascii=False)}")

        if self._request_backend == "node":
            result = await self._call_generate_api_undici(payload)
        else:
            result = await self._call_generate_api_httpx(payload, headers)

        if not result.get("success"):
            raise RuntimeError(result.get("error") or result.get("message") or "云端接口返回失败")

        data = result.get("data") or {}
        image_url = data.get("image_url")
        if not image_url:
            raise RuntimeError("接口返回成功，但缺少 image_url")

        return {
            "image_url": image_url,
            "image_id": data.get("image_id", ""),
            "model_name": data.get("model_name") or self.MODEL_CHOICES.get(model_index, f"模型{model_index}"),
            "points_used": data.get("points_used", "?"),
            "remaining_points": data.get("remaining_points", "?"),
            "raw_response": result,
        }

    async def _extract_image_url_from_event(self, event: AstrMessageEvent) -> str:
        """从消息或回复链中提取第一张图片 URL"""
        try:
            for seg in event.message_obj.message:
                if isinstance(seg, Reply) and getattr(seg, "chain", None):
                    for reply_seg in seg.chain:
                        if isinstance(reply_seg, AstrImageComponent) and getattr(reply_seg, "url", ""):
                            return reply_seg.url
            for seg in event.message_obj.message:
                if isinstance(seg, AstrImageComponent) and getattr(seg, "url", ""):
                    return seg.url
        except Exception as e:
            logger.warning(f"[NovaCloudComfyUI] 提取消息图片失败: {e}")
        return ""

    def _guess_file_extension(self, content_type: str) -> str:
        ext = (content_type or "image/jpeg").split("/")[-1].split(";")[0].lower()
        if ext == "svg+xml":
            ext = "svg"
        if ext not in ("png", "jpg", "jpeg", "webp", "gif", "bmp", "svg"):
            ext = "jpg"
        return ext

    def _read_image_size(self, filepath: Path) -> tuple[int, int]:
        data = filepath.read_bytes()

        if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            width = int.from_bytes(data[16:20], "big")
            height = int.from_bytes(data[20:24], "big")
            return width, height

        if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
            width = int.from_bytes(data[6:8], "little")
            height = int.from_bytes(data[8:10], "little")
            return width, height

        if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk_type = data[12:16]
            if chunk_type == b"VP8X" and len(data) >= 30:
                width = 1 + int.from_bytes(data[24:27], "little")
                height = 1 + int.from_bytes(data[27:30], "little")
                return width, height
            if chunk_type == b"VP8 " and len(data) >= 30:
                start = data.find(b"\x9d\x01\x2a")
                if start != -1 and start + 7 < len(data):
                    width = int.from_bytes(data[start + 3:start + 5], "little") & 0x3FFF
                    height = int.from_bytes(data[start + 5:start + 7], "little") & 0x3FFF
                    return width, height
            if chunk_type == b"VP8L" and len(data) >= 25:
                b0, b1, b2, b3 = data[21:25]
                width = 1 + (((b1 & 0x3F) << 8) | b0)
                height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
                return width, height

        if len(data) >= 4 and data[:2] == b"\xff\xd8":
            offset = 2
            while offset + 9 < len(data):
                if data[offset] != 0xFF:
                    offset += 1
                    continue
                marker = data[offset + 1]
                offset += 2
                if marker in (0xD8, 0xD9):
                    continue
                if offset + 2 > len(data):
                    break
                block_size = int.from_bytes(data[offset:offset + 2], "big")
                if block_size < 2 or offset + block_size > len(data):
                    break
                if marker in {
                    0xC0, 0xC1, 0xC2, 0xC3,
                    0xC5, 0xC6, 0xC7,
                    0xC9, 0xCA, 0xCB,
                    0xCD, 0xCE, 0xCF,
                }:
                    height = int.from_bytes(data[offset + 3:offset + 5], "big")
                    width = int.from_bytes(data[offset + 5:offset + 7], "big")
                    return width, height
                offset += block_size

        return 0, 0

    def _save_downloaded_bytes(self, content: bytes, content_type: str) -> Path:
        ext = self._guess_file_extension(content_type)
        filename = f"{uuid.uuid4()}.{ext}"
        filepath = self.image_dir / filename
        filepath.write_bytes(content)
        logger.info(f"[NovaCloudComfyUI] 图片已保存: {filepath}")
        return filepath

    def _convert_image_to_png(self, filepath: Path) -> Path:
        target_path = filepath.with_suffix(".png")
        with PILImage.open(filepath) as img:
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            img.save(target_path, format="PNG")
        logger.info(f"[NovaCloudComfyUI] 图片已转换为 PNG: {target_path}")
        return target_path

    def _get_moderation_credentials(self) -> list[tuple[str, str]]:
        credentials = []
        for item in self.config.get("sightengine_credentials", []):
            api_user, separator, api_secret = str(item).partition(":")
            if separator and api_user.strip() and api_secret.strip():
                credentials.append((api_user.strip(), api_secret.strip()))
        return credentials

    def _next_moderation_credential(self) -> tuple[str, str]:
        credentials = self._get_moderation_credentials()
        if not credentials:
            raise RuntimeError("未配置 Sightengine 凭据")
        credential = credentials[self._moderation_credential_index % len(credentials)]
        self._moderation_credential_index = (
            self._moderation_credential_index + 1
        ) % len(credentials)
        return credential

    async def _is_sensitive_image(self, image_path: Path) -> bool:
        api_url = str(
            self.config.get(
                "moderation_api_url",
                "https://api.sightengine.com/1.0/check.json",
            )
        ).strip()
        retries = max(0, int(self.config.get("moderation_retries", 2)))
        threshold = min(
            1.0,
            max(0.0, float(self.config.get("moderation_threshold", 0.8))),
        )

        for attempt in range(1, retries + 2):
            credential_label = "unavailable"
            try:
                api_user, api_secret = self._next_moderation_credential()
                credential_label = api_user
                if self._moderation_client is None:
                    raise RuntimeError("图片审核客户端未初始化")
                response = await self._moderation_client.post(
                    api_url,
                    data={
                        "models": "nudity-2.1",
                        "api_user": api_user,
                        "api_secret": api_secret,
                    },
                    files={
                        "media": (
                            image_path.name,
                            image_path.read_bytes(),
                            "application/octet-stream",
                        )
                    },
                )
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}")
                result = response.json()
                if not isinstance(result, dict) or result.get("status") != "success":
                    error = result.get("error", {}) if isinstance(result, dict) else {}
                    raise RuntimeError(
                        str(error.get("message") or "Sightengine 响应状态异常")
                    )
                nudity = result.get("nudity")
                if not isinstance(nudity, dict):
                    raise RuntimeError("响应缺少 nudity 数据")

                scores = {
                    key: float(nudity.get(key, 0.0))
                    for key in ("sexual_activity", "sexual_display", "erotica")
                }
                max_score = max(scores.values())
                is_sensitive = max_score >= threshold
                logger.info(
                    f"[NovaCloudComfyUI] 图片审核完成: sensitive={is_sensitive}, "
                    f"score={max_score:.3f}, threshold={threshold:.3f}, "
                    f"attempt={attempt}/{retries + 1}, credential={credential_label}"
                )
                return is_sensitive
            except (
                httpx.HTTPError,
                asyncio.TimeoutError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as e:
                logger.warning(
                    f"[NovaCloudComfyUI] 图片审核失败: attempt={attempt}/{retries + 1}, "
                    f"credential={credential_label}, error={type(e).__name__}: {e}"
                )

        logger.warning("[NovaCloudComfyUI] 图片审核重试全部失败，安全兜底为合并转发")
        return True

    @staticmethod
    def _clean_model_name(model_name: Any) -> str:
        cleaned = re.sub(r"^(?:\s*\[[^\]]*\])+\s*", "", str(model_name or "未知"))
        return cleaned.strip() or "未知"

    @classmethod
    def _build_result_summary(cls, result: dict[str, Any]) -> str:
        model_name = cls._clean_model_name(result.get("model_name", "未知"))
        return (
            "绘图结果\n"
            f"模型: {model_name}\n"
            f"尺寸: {result.get('width')}x{result.get('height')}"
        )

    @staticmethod
    def _format_user_error(error: Exception) -> str:
        message = str(error)
        status_match = re.search(
            r"(?:请求失败\s*\(|HTTP\s*)([1-5]\d{2})",
            message,
            re.IGNORECASE,
        )
        if status_match:
            return status_match.group(1)
        return message[:200]

    async def _send_image_result(
        self,
        event: AstrMessageEvent,
        image_path: Path,
        *,
        use_forward: bool,
        summary_text: str,
    ) -> None:
        if (
            not use_forward
            and bool(self.config.get("smart_forward_sensitive_images", False))
        ):
            use_forward = await self._is_sensitive_image(image_path)

        if not use_forward:
            await event.send(event.chain_result([Image.fromFileSystem(str(image_path))]))
            return

        raw_message = getattr(event.message_obj, "raw_message", None)
        sender_name = getattr(event.get_sender_name(), "strip", lambda: "")() or "Nova"
        sender_id = event.get_sender_id()

        image_bytes = image_path.read_bytes()
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        image_ext = image_path.suffix.lower().lstrip(".") or "png"
        if image_ext == "jpg":
            image_ext = "jpeg"
        image_data_uri = f"data:image/{image_ext};base64,{image_base64}"

        node = {
            "type": "node",
            "data": {
                "name": sender_name,
                "uin": int(sender_id) if str(sender_id).isdigit() else 10000,
                "content": [
                    {"type": "text", "data": {"text": summary_text}},
                    {"type": "image", "data": {"file": image_data_uri}},
                ],
            },
        }

        if isinstance(raw_message, dict) and raw_message.get("group_id"):
            await event.bot.send_group_forward_msg(
                group_id=int(raw_message.get("group_id")),
                messages=[node],
            )
        else:
            await event.bot.send_private_forward_msg(
                user_id=int(sender_id),
                messages=[node],
            )

    async def _download_image_via_undici(self, image_url: str) -> Path:
        bridge_path = self.plugin_dir / "undici_bridge.mjs"
        if not bridge_path.exists():
            raise RuntimeError("undici_bridge.mjs 不存在，无法使用 Node 下载桥接")

        command = [
            "node",
            str(bridge_path),
            "--url",
            image_url,
            "--method",
            "GET",
            "--response-type",
            "base64",
        ]

        if self._proxy_url:
            command.extend(["--proxy", self._proxy_url])

        logger.info(f"[NovaCloudComfyUI] 使用 undici 桥接下载图片: {image_url[:120]}...")
        result = subprocess.run(
            command,
            cwd=str(self.plugin_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=int(self.config.get("timeout", 180)) + 10,
            check=False,
        )

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            raise RuntimeError(f"undici 下载桥接执行失败: {stderr or stdout or '未知错误'}")

        try:
            bridge_result = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"undici 下载桥接响应解析失败: {stdout[:500]}") from exc

        if "error" in bridge_result:
            raise RuntimeError(f"undici 下载桥接错误: {bridge_result.get('error')}")

        status_code = int(bridge_result.get("status", 0) or 0)
        if status_code != 200:
            raise RuntimeError(f"undici 下载图片失败: HTTP {status_code}")

        body_base64 = bridge_result.get("body") or ""
        if not isinstance(body_base64, str) or not body_base64:
            raise RuntimeError("undici 下载桥接返回的图片内容为空")

        try:
            content = base64.b64decode(body_base64)
        except Exception as exc:
            raise RuntimeError("undici 下载桥接返回的 base64 数据无效") from exc

        headers = bridge_result.get("headers") or {}
        content_type = headers.get("content-type", "image/jpeg")
        return self._save_downloaded_bytes(content, content_type)

    async def _download_image_via_httpx(self, image_url: str) -> Path:
        logger.info(f"[NovaCloudComfyUI] 使用 HTTPX 下载图片: {image_url[:120]}...")
        response = await self._client.get(image_url)

        if response.status_code != 200:
            raise RuntimeError(f"下载图片失败: HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "image/jpeg")
        return self._save_downloaded_bytes(response.content, content_type)

    async def _download_image(self, image_url: str) -> Path:
        logger.info(f"[NovaCloudComfyUI] 下载图片: {image_url[:120]}...")
        
        if self._request_backend == "node":
            try:
                return await self._download_image_via_undici(image_url)
            except Exception as e:
                logger.warning(f"[NovaCloudComfyUI] undici 下载失败，回退 HTTPX: {e}")
                return await self._download_image_via_httpx(image_url)
        else:
            try:
                return await self._download_image_via_httpx(image_url)
            except Exception as e:
                logger.warning(f"[NovaCloudComfyUI] HTTPX 下载失败，回退 undici: {e}")
                return await self._download_image_via_undici(image_url)

    def _extract_model_token(self, text: str) -> tuple[str, str]:
        match = re.search(r"\bmodel\s*(\d{1,2})\b", text, re.IGNORECASE)
        if not match:
            return text.strip(), ""
        model_index = match.group(1)
        cleaned = re.sub(r"\bmodel\s*\d{1,2}\b", "", text, flags=re.IGNORECASE).strip()
        logger.info(
            f"[NovaCloudComfyUI] 从 prompt 中提取模型标记: model{model_index} -> 清洗后 prompt: {cleaned}"
        )
        return cleaned, model_index

    def _extract_resolution_token(self, text: str) -> tuple[str, str]:
        parts = text.rsplit(maxsplit=1)
        if len(parts) != 2:
            return text.strip(), ""

        possible_prompt, suffix = parts
        suffix_lower = suffix.lower()
        if suffix_lower in self.RESOLUTION_PRESETS or re.match(r"\d+\s*[x×*X-]\s*\d+", suffix_lower):
            logger.info(
                f"[NovaCloudComfyUI] 从 prompt 中提取分辨率标记: {suffix_lower} -> 清洗后 prompt: {possible_prompt.strip()}"
            )
            return possible_prompt.strip(), suffix_lower
        return text.strip(), ""

    def _extract_cfg_token(self, text: str) -> tuple[str, str]:
        match = re.search(r"\bcfg\s*([0-9]+(?:\.[0-9]+)?)\b", text, re.IGNORECASE)
        if not match:
            return text.strip(), ""
        cfg_value = match.group(1)
        cleaned = re.sub(r"\bcfg\s*[0-9]+(?:\.[0-9]+)?\b", "", text, flags=re.IGNORECASE).strip()
        logger.info(
            f"[NovaCloudComfyUI] 从 prompt 中提取 CFG 标记: cfg{cfg_value} -> 清洗后 prompt: {cleaned}"
        )
        return cleaned, cfg_value

    def _extract_steps_token(self, text: str) -> tuple[str, str]:
        match = re.search(r"\bsteps\s*(\d{1,3})\b", text, re.IGNORECASE)
        if not match:
            return text.strip(), ""
        steps_value = match.group(1)
        cleaned = re.sub(r"\bsteps\s*\d{1,3}\b", "", text, flags=re.IGNORECASE).strip()
        logger.info(
            f"[NovaCloudComfyUI] 从 prompt 中提取步数标记: steps{steps_value} -> 清洗后 prompt: {cleaned}"
        )
        return cleaned, steps_value

    def _extract_seed_token(self, text: str) -> tuple[str, str]:
        match = re.search(r"\bseed\s*(\d{1,10})\b", text, re.IGNORECASE)
        if not match:
            return text.strip(), ""
        seed_value = match.group(1)
        cleaned = re.sub(r"\bseed\s*\d{1,10}\b", "", text, flags=re.IGNORECASE).strip()
        logger.info(
            f"[NovaCloudComfyUI] 从 prompt 中提取种子标记: seed{seed_value} -> 清洗后 prompt: {cleaned}"
        )
        return cleaned, seed_value

    def _inject_quality_tags(self, prompt: str, model_index: int) -> str:
        if self._is_edit_model_index(model_index):
            return prompt.strip()

        existing_tags = {tag.strip().lower() for tag in prompt.split(",") if tag.strip()}
        final_tags = [tag.strip() for tag in prompt.split(",") if tag.strip()]

        for tag in self.QUALITY_PROMPT_TAGS:
            if tag.lower() not in existing_tags:
                final_tags.append(tag)

        return ", ".join(final_tags)

    def _scale_edit_image_to_limit(self, width: int, height: int) -> tuple[int, int]:
        if width <= 0 or height <= 0:
            return width, height

        max_side_limit = self.AUTO_EDIT_TARGET_MAX_SIDE
        longest_side = max(width, height)
        if longest_side <= 0:
            return width, height

        scale = max_side_limit / longest_side
        target_width = width * scale
        target_height = height * scale

        width_candidates = {
            max(64, min(max_side_limit, int(target_width))),
            max(64, min(max_side_limit, int(target_width + 0.999999))),
        }
        height_candidates = {
            max(64, min(max_side_limit, int(target_height))),
            max(64, min(max_side_limit, int(target_height + 0.999999))),
        }

        source_ratio = width / height
        best = None
        best_score = None

        for cand_width in width_candidates:
            for cand_height in height_candidates:
                if cand_width > max_side_limit or cand_height > max_side_limit:
                    continue
                ratio_diff = abs((cand_width / cand_height) - source_ratio)
                side_diff = abs(max(cand_width, cand_height) - max_side_limit)
                score = (round(ratio_diff, 12), side_diff, -cand_width * cand_height)
                if best is None or score < best_score:
                    best = (cand_width, cand_height)
                    best_score = score

        return best or (max(64, min(max_side_limit, int(target_width))), max(64, min(max_side_limit, int(target_height))))

    async def _run_draw_request(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        negative_prompt: str = "",
        resolution: str = "",
        steps: str | int = "",
        cfg: str | float = "",
        model_index: str | int | None = "",
        seed: str | int = "",
        image_source: str = "",
        force_edit: bool = False,
        auto_send: bool = True,
    ) -> dict[str, Any]:
        if not prompt:
            raise RuntimeError("请提供 prompt 提示词")

        prompt_text = prompt.strip()
        raw_prompt_text = prompt_text
        prompt_text, extracted_model = self._extract_model_token(prompt_text)
        prompt_text, extracted_cfg = self._extract_cfg_token(prompt_text)
        prompt_text, extracted_steps = self._extract_steps_token(prompt_text)
        prompt_text, extracted_seed = self._extract_seed_token(prompt_text)
        prompt_text, extracted_resolution = self._extract_resolution_token(prompt_text)

        actual_model_input = extracted_model or model_index
        actual_resolution = resolution or extracted_resolution
        actual_cfg_input = cfg or extracted_cfg
        actual_steps_input = steps or extracted_steps
        actual_seed_input = seed or extracted_seed

        actual_model = self._normalize_model_index(actual_model_input)
        if force_edit and not self._is_edit_model_index(actual_model):
            actual_model = self._get_default_edit_model_index()

        width, height = self._parse_resolution(actual_resolution or None)
        actual_steps = self._normalize_steps(actual_steps_input, actual_model)
        actual_cfg = self._normalize_cfg(actual_cfg_input, actual_model)
        actual_seed = self._normalize_seed(actual_seed_input, actual_model)
        actual_neg = negative_prompt or self.config.get("default_negative_prompt", "")
        actual_image_source = image_source.strip()

        if self._is_edit_model_index(actual_model) and not actual_image_source:
            actual_image_source = await self._extract_image_url_from_event(event)

        auto_shrink_edit_resolution = bool(self.config.get("auto_shrink_edit_resolution", True))
        auto_enlarge_edit_resolution = bool(self.config.get("auto_enlarge_edit_resolution", False))
        if self._is_edit_model_index(actual_model) and not actual_resolution and actual_image_source:
            try:
                source_path = await self._download_image(actual_image_source)
                src_width, src_height = self._read_image_size(source_path)
                if src_width > 0 and src_height > 0:
                    longest_side = max(src_width, src_height)
                    should_shrink = longest_side > self.AUTO_EDIT_TARGET_MAX_SIDE and auto_shrink_edit_resolution
                    should_enlarge = longest_side < self.AUTO_EDIT_TARGET_MAX_SIDE and auto_enlarge_edit_resolution
                    if should_shrink or should_enlarge:
                        width, height = self._scale_edit_image_to_limit(src_width, src_height)
                        actual_resolution = f"{width}x{height}"
                        action = "缩小" if should_shrink else "放大"
                        logger.info(
                            f"[NovaCloudComfyUI] 编辑模型未指定分辨率，已按面板开关自动等比{action}到单边不超过"
                            f"{self.AUTO_EDIT_TARGET_MAX_SIDE}: source={src_width}x{src_height}, "
                            f"target={width}x{height}, shrink={auto_shrink_edit_resolution}, "
                            f"enlarge={auto_enlarge_edit_resolution}"
                        )
                    else:
                        width, height = src_width, src_height
                        actual_resolution = f"{width}x{height}"
                        logger.info(
                            f"[NovaCloudComfyUI] 编辑模型未指定分辨率，未触发自动缩放，直接使用原图分辨率: "
                            f"source={src_width}x{src_height}, shrink={auto_shrink_edit_resolution}, "
                            f"enlarge={auto_enlarge_edit_resolution}"
                        )
            except Exception as e:
                logger.warning(f"[NovaCloudComfyUI] 读取原图尺寸失败，回退默认编辑分辨率: {e}")

        prompt_text = self._inject_quality_tags(prompt_text, actual_model)

        logger.info(
            f"[NovaCloudComfyUI] prompt 解析结果: raw_prompt={raw_prompt_text!r}, "
            f"clean_prompt={prompt_text!r}, extracted_model={extracted_model or 'none'}, "
            f"extracted_cfg={extracted_cfg or 'none'}, extracted_steps={extracted_steps or 'none'}, "
            f"extracted_seed={extracted_seed or 'none'}, extracted_resolution={extracted_resolution or 'none'}"
        )
        logger.info(
            f"[NovaCloudComfyUI] 实际请求参数: model={actual_model}, size={width}x{height}, "
            f"steps={actual_steps}, cfg={actual_cfg}, seed={actual_seed}"
        )

        result = await self._call_generate_api(
            prompt=prompt_text,
            negative_prompt=actual_neg,
            width=width,
            height=height,
            steps=actual_steps,
            cfg=actual_cfg,
            model_index=actual_model,
            seed=actual_seed,
            image_source=actual_image_source,
        )

        image_path = await self._download_image(result["image_url"])

        convert_edit_result_to_png = bool(self.config.get("convert_edit_result_to_png", True))
        if self._is_edit_model_index(actual_model) and convert_edit_result_to_png:
            try:
                image_path = self._convert_image_to_png(image_path)
            except Exception as e:
                logger.warning(f"[NovaCloudComfyUI] 编辑结果转 PNG 失败，继续发送原图: {e}")

        real_width, real_height = self._read_image_size(image_path)

        result["image_path"] = str(image_path)
        result["requested_width"] = width
        result["requested_height"] = height
        result["width"] = real_width or width
        result["height"] = real_height or height
        result["actual_model"] = actual_model
        result["actual_image_source"] = actual_image_source
        result["actual_prompt"] = prompt_text
        result["actual_resolution"] = actual_resolution
        result["actual_seed"] = actual_seed

        if auto_send:
            await self._send_image_result(
                event,
                image_path,
                use_forward=bool(self.config.get("send_image_as_forward", False)),
                summary_text=self._build_result_summary(result),
            )

        return result

    @filter.llm_tool()
    async def nova_draw_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
    ):
        """ComfyUI 文生图工具。仅当用户明确提到用comfyui生图、用comfyui画图、画色图、涩图时调用。文生图 prompt 必须是详细英文 tag 串，不要用中文自然语言。

        Args:
            prompt(string): 英文tag形式的文生图提示词。可在末尾附带 model10、model14、1216x832、1216-832 这类模型和分辨率标记；其余默认值如 steps=28、cfg=5、negative_prompt 由插件内部和面板配置处理
        """
        user_id = event.get_sender_id()
        request_id = f"cloudcomfyui_{user_id}"

        if self._check_debounce(user_id):
            interval = self.config.get("debounce_interval", 15)
            return f"请等待 {interval} 秒后再试"

        if request_id in self._processing_users:
            return "您有正在进行的生图任务，请稍候..."

        self._processing_users.add(request_id)

        try:
            await event.send(event.plain_result("正在使用comfyui画图，请稍后..."))
            result = await self._run_draw_request(
                event=event,
                prompt=prompt,
            )
            return self._build_result_summary(result)
        except Exception as e:
            logger.error(f"[NovaCloudComfyUI] 主动文生图失败: {e}")
            return f"生成图片失败: {self._format_user_error(e)}"
        finally:
            self._processing_users.discard(request_id)

    @filter.llm_tool()
    async def nova_edit_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        use_message_images: bool = True,
        image_source: str = "",
    ):
        """ComfyUI 图片编辑工具。仅当用户明确提到用comfyui改图，或修改色图、涩图时调用。edit prompt 可以直接使用中文自然语言。

        Args:
            prompt(string): 中文自然语言编辑指令，例如 把头发改成黑发，保持人物脸部、服装和构图不变
            use_message_images(boolean): 是否自动使用当前消息或引用消息中的图片，默认 true。推荐优先使用这个方式取图
            image_source(string): 可直接访问的图片URL；若为空则尝试从消息中自动提取。默认编辑模型索引由面板配置 default_edit_model_index 决定，默认值为 14
        """
        user_id = event.get_sender_id()
        request_id = f"cloudcomfyui_{user_id}"

        if self._check_debounce(user_id):
            interval = self.config.get("debounce_interval", 15)
            return f"请等待 {interval} 秒后再试"

        if request_id in self._processing_users:
            return "您有正在进行的改图任务，请稍候..."

        self._processing_users.add(request_id)

        try:
            await event.send(event.plain_result("正在使用comfyui画图，请稍后..."))
            actual_image_source = image_source.strip()
            if use_message_images and not actual_image_source:
                actual_image_source = await self._extract_image_url_from_event(event)

            if not actual_image_source:
                return "请先发送图片，或回复一张图片后再告诉我怎么修改。"

            result = await self._run_draw_request(
                event=event,
                prompt=prompt,
                model_index=str(self._get_default_edit_model_index()),
                image_source=actual_image_source,
                force_edit=True,
            )
            return self._build_result_summary(result)
        except Exception as e:
            logger.error(f"[NovaCloudComfyUI] 主动图编辑失败: {e}")
            return f"编辑图片失败: {self._format_user_error(e)}"
        finally:
            self._processing_users.discard(request_id)

    @filter.command("comfyui", alias={"色图", "涩图", "画图"})
    async def comfyui_command(self, event: AstrMessageEvent):
        """生成图片指令

        用法示例：
        - /comfyui 1girl, silver hair, red eyes portrait
        - /comfyui 1girl, city night 1216x832
        - /comfyui 把这张图头发改成黑发
        - /comfyui 把这张图背景改成海边 --image_source=https://example.com/a.png

        说明：
        - 直接在 prompt 里写 model8、model10、model14 这类模型标记即可，插件会自动提取并删除
        - 直接在提示词最后写分辨率即可，如 portrait、landscape、square、1024、1216x832
        - 如果消息里带图、引用了图片，或提供了 --image_source，插件会自动切到面板配置的默认编辑模型索引
        """
        arg = event.message_str.partition(" ")[2].strip()
        if not arg:
            yield event.plain_result(
                "请提供提示词！\n"
                "用法: /comfyui <提示词> [分辨率]\n"
                "示例1: /comfyui 1girl, silver hair portrait\n"
                "示例2: /comfyui 把这张图头发改成黑发 --image_source=https://example.com/a.jpg"
            ).stop_event()
            return

        resolution = ""
        image_source = ""
        explicit_model: int | None = None

        model_match = re.search(r"\bmodel\s*(\d{1,2})\b", arg, re.IGNORECASE)
        if model_match:
            explicit_model = int(model_match.group(1))
            arg = re.sub(r"\bmodel\s*\d{1,2}\b", "", arg, flags=re.IGNORECASE).strip()

        image_source_match = re.search(r"--image_source=(\S+)", arg, re.IGNORECASE)
        if image_source_match:
            image_source = image_source_match.group(1).strip()
            arg = re.sub(r"\s*--image_source=\S+", "", arg, flags=re.IGNORECASE).strip()

        parts = arg.rsplit(maxsplit=1)
        if len(parts) == 2:
            possible_prompt, suffix = parts
            suffix_lower = suffix.lower()
            if suffix_lower in self.RESOLUTION_PRESETS or re.match(r"\d+\s*[x×*X-]\s*\d+", suffix_lower):
                prompt = possible_prompt
                resolution = suffix_lower
            else:
                prompt = arg
        else:
            prompt = arg

        user_id = event.get_sender_id()
        request_id = f"cloudcomfyui_{user_id}"

        if self._check_debounce(user_id):
            interval = self.config.get("debounce_interval", 15)
            yield event.plain_result(f"请等待 {interval} 秒后再试").stop_event()
            return

        if request_id in self._processing_users:
            yield event.plain_result("您有正在进行的生图任务，请稍候...").stop_event()
            return

        self._processing_users.add(request_id)

        try:
            await event.send(event.plain_result("正在使用comfyui画图，请稍后..."))
            has_image_context = bool(image_source) or bool(await self._extract_image_url_from_event(event))
            actual_model = explicit_model if explicit_model is not None else self._normalize_model_index(None)
            force_edit = has_image_context and explicit_model is None
            if force_edit:
                actual_model = self._get_default_edit_model_index()

            result = await self._run_draw_request(
                event=event,
                prompt=prompt,
                resolution=resolution,
                model_index=str(actual_model),
                image_source=image_source,
                force_edit=force_edit or self._is_edit_model_index(actual_model),
                auto_send=False,
            )

            image_path = result.get("image_path", "")
            logger.info(
                f"[NovaCloudComfyUI] 指令生图成功: model={result.get('model_name', '未知')}, "
                f"size={result.get('width')}x{result.get('height')}, "
                f"points={result.get('points_used', '?')}/{result.get('remaining_points', '?')}, "
                f"seed={result.get('actual_seed', '?')}, image_path={image_path}"
            )
            if image_path:
                await self._send_image_result(
                    event,
                    Path(image_path),
                    use_forward=bool(self.config.get("send_image_as_forward", False)),
                    summary_text=self._build_result_summary(result),
                )
                yield event.plain_result("").stop_event()
            else:
                yield event.plain_result("生成成功，但图片文件路径为空").stop_event()

        except Exception as e:
            logger.error(f"[NovaCloudComfyUI] 指令生图失败: {repr(e)}")
            yield event.plain_result(
                f"生成图片失败: {self._format_user_error(e)}"
            ).stop_event()

        finally:
            self._processing_users.discard(request_id)