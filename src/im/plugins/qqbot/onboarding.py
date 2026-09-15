"""QQ official bot QR onboarding.

This mirrors Hermes' scan-to-configure flow: create a bind task, expose the
mobile QR URL, poll until completion, then decrypt the returned secret locally.
The module is transport-only; the generic web API owns the task lifecycle.
"""

import base64
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

PORTAL_HOST = os.getenv("QQ_PORTAL_HOST", "q.qq.com")
CREATE_PATH = "/lite/create_bind_task"
POLL_PATH = "/lite/poll_bind_result"
QR_TEMPLATE = "https://q.qq.com/qqbot/openclaw/connect.html?task_id={}&_wv=2&source=wx-assist"
POLL_INTERVAL = 2.0
REQUEST_TIMEOUT = 10.0


@dataclass
class QQOnboardingTask:
    task_id: str
    key: str
    qr_url: str
    created_at: float
    expires_at: float
    status: str = "pending"
    result: Optional[dict] = None
    error: str = ""


class QQOnboarding:
    def __init__(self):
        self._tasks: dict[str, QQOnboardingTask] = {}
        self._lock = threading.RLock()

    def start(self, timeout_seconds: int = 600) -> dict:
        key = base64.b64encode(os.urandom(32)).decode()
        response = requests.post(
            f"https://{PORTAL_HOST}{CREATE_PATH}",
            json={"key": key},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("retcode") != 0:
            raise RuntimeError(data.get("msg", "QQ bind task failed"))
        task_id = str((data.get("data") or {}).get("task_id", ""))
        if not task_id:
            raise RuntimeError("QQ bind task missing task_id")
        now = time.time()
        task = QQOnboardingTask(
            task_id=task_id,
            key=key,
            qr_url=QR_TEMPLATE.format(quote(task_id)),
            created_at=now,
            expires_at=now + timeout_seconds,
        )
        with self._lock:
            self._tasks[task_id] = task
        return {"task_id": task_id, "qr_url": task.qr_url, "expires_at": task.expires_at}

    def poll(self, task_id: str) -> dict:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            logger.error("[qq-onboard] poll: task not found: %s", task_id)
            raise KeyError("QQ onboarding task not found")
        if time.time() >= task.expires_at:
            task.status = "expired"
            logger.warning("[qq-onboard] poll: task expired: %s", task_id)
            return {"status": task.status}
        try:
            response = requests.post(
                f"https://{PORTAL_HOST}{POLL_PATH}",
                json={"task_id": task_id},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.error("[qq-onboard] poll request failed: %s", exc)
            raise
        if data.get("retcode") != 0:
            retcode = data.get("retcode")
            msg = data.get("msg", "QQ bind poll failed")
            logger.error("[qq-onboard] poll retcode=%s msg=%s", retcode, msg)
            # 30012 轮询频率过高是瞬态限流：按 pending 返回，UI 继续轮询即可
            if retcode == 30012:
                return {"status": task.status, "rate_limited": True}
            raise RuntimeError(msg)
        payload = data.get("data") or {}
        status = int(payload.get("status", 0))
        logger.info("[qq-onboard] poll task_id=%s status=%s retcode=%s", task_id, status, data.get("retcode"))
        if status == 2:
            encrypted = str(payload.get("bot_encrypt_secret", ""))
            try:
                client_secret = self._decrypt(encrypted, task.key)
            except ImportError as exc:
                # 依赖缺失（如 cryptography 未安装）——明确失败，让 UI 停止轮询并提示
                task.status = "failed"
                task.error = f"服务端缺少解密依赖（{exc.name or 'cryptography'}），请在项目环境执行 pip install cryptography 后重试"
                logger.exception("[qq-onboard] decrypt dependency missing")
                return {"status": task.status, "error": task.error}
            except Exception as exc:
                task.status = "failed"
                task.error = f"扫码成功但密钥解密失败：{exc}"
                logger.exception("[qq-onboard] decrypt failed")
                return {"status": task.status, "error": task.error}
            result = {
                "app_id": str(payload.get("bot_appid", "")),
                "client_secret": client_secret,
                "user_openid": str(payload.get("user_openid", "")),
            }
            task.status = "completed"
            task.result = result
            logger.info("[qq-onboard] completed: app_id=%s user_openid=%s", result["app_id"], result["user_openid"])
            return {"status": task.status, **result}
        if status == 3:
            task.status = "expired"
            logger.warning("[qq-onboard] task expired by server: %s", task_id)
        return {"status": task.status}

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.status = "cancelled"
            return True

    @staticmethod
    def _decrypt(encrypted: str, key: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.b64decode(encrypted)
        key_bytes = base64.b64decode(key)
        return AESGCM(key_bytes).decrypt(raw[:12], raw[12:], None).decode("utf-8")


_onboarding = QQOnboarding()


def get_qq_onboarding() -> QQOnboarding:
    return _onboarding
