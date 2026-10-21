import uuid
import datetime
import json
import logging
from pathlib import Path
from typing import List, Dict, Literal, Optional
from filelock import FileLock, Timeout

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
NOTIFICATIONS_FILE = DATA_DIR / "notifications.json"
NOTIFICATIONS_LOCK = DATA_DIR / "notifications.json.lock"

class NotificationStore:
    def __init__(self):
        self.last_category_run: Optional[str] = None
        self.last_price_run: Optional[str] = None
        self.max_notifications = 50
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not NOTIFICATIONS_FILE.exists():
            self._save([])

    def _load(self) -> List[Dict]:
        try:
            with FileLock(str(NOTIFICATIONS_LOCK), timeout=2):
                if not NOTIFICATIONS_FILE.exists():
                    return []
                with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
                    notifications = json.load(f)
                # Auto-cleanup: mark old notifications as read, remove very old ones
                now = datetime.datetime.now(datetime.timezone.utc)
                cleaned = []
                changed = False
                for n in notifications:
                    ts = n.get("timestamp")
                    if ts:
                        try:
                            created = datetime.datetime.fromisoformat(
                                str(ts).replace("Z", "+00:00")
                            )
                            age = now - created
                            if age.days > 30:
                                changed = True
                                continue  # drop very old notifications
                            if age.days > 7 and not n.get("read"):
                                n["read"] = True
                                changed = True
                        except (ValueError, TypeError):
                            pass
                    cleaned.append(n)
                if changed:
                    with open(NOTIFICATIONS_FILE, "w", encoding="utf-8") as f:
                        json.dump(cleaned, f, ensure_ascii=False, indent=2)
                return cleaned
        except Exception as e:
            logger.error(f"Erro ao carregar notificações: {e}")
            return []

    def _save(self, notifications: List[Dict]):
        try:
            with FileLock(str(NOTIFICATIONS_LOCK), timeout=2):
                with open(NOTIFICATIONS_FILE, "w", encoding="utf-8") as f:
                    json.dump(notifications, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Erro ao salvar notificações: {e}")

    def add_notification(
        self,
        title: str,
        message: str,
        tab: str,
        *,
        route_kind: Optional[Literal["search", "cross", "monitor", "category-monitor"]] = None,
        route_job_id: Optional[str] = None,
        route_monitor_id: Optional[str] = None,
        route_product_url: Optional[str] = None,
    ):
        if route_kind is None:
            if route_job_id is not None or route_monitor_id is not None or route_product_url is not None:
                raise ValueError("route identifiers require a route_kind")
            route_target = None
        elif route_kind in ("search", "cross"):
            if route_monitor_id is not None or route_product_url is not None or not isinstance(route_job_id, str) or not route_job_id.strip():
                raise ValueError("search and cross targets require only a non-empty route_job_id")
            route_target = {"kind": route_kind, "jobId": route_job_id}
        elif route_kind == "monitor":
            if route_job_id is not None or route_product_url is not None or not isinstance(route_monitor_id, str) or not route_monitor_id.strip():
                raise ValueError("monitor targets require only a non-empty route_monitor_id")
            route_target = {"kind": "monitor", "monitorId": route_monitor_id}
        elif route_kind == "category-monitor":
            if route_job_id is not None or not isinstance(route_monitor_id, str) or not route_monitor_id.strip() or not isinstance(route_product_url, str) or not route_product_url.strip():
                raise ValueError("category-monitor targets require non-empty monitor and product URL")
            route_target = {
                "kind": "category-monitor",
                "monitorId": route_monitor_id,
                "productUrl": route_product_url,
            }
        else:
            raise ValueError("route_kind must be 'search', 'cross', 'monitor', or 'category-monitor'")
        
        notifications = self._load()
        notifications.insert(0, {
            "id": str(uuid.uuid4()),
            "title": title,
            "message": message,
            "tab": tab,
            "read": False,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            **({"route_target": route_target} if route_target is not None else {}),
        })
        
        if len(notifications) > self.max_notifications:
            notifications = notifications[:self.max_notifications]
            
        self._save(notifications)

    def mark_as_read(self, notif_id: str):
        notifications = self._load()
        changed = False
        for notif in notifications:
            if notif["id"] == notif_id:
                notif["read"] = True
                changed = True
                break
        if changed:
            self._save(notifications)

    def mark_all_as_read(self):
        notifications = self._load()
        changed = False
        for notif in notifications:
            if notif.get("read") is not True:
                notif["read"] = True
                changed = True
        if changed:
            self._save(notifications)

    def get_unread(self):
        return [n for n in self._load() if not n["read"]]

    def get_all(self):
        return self._load()

    def set_last_category_run(self):
        self.last_category_run = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def set_last_price_run(self):
        self.last_price_run = datetime.datetime.now(datetime.timezone.utc).isoformat()

# Global singleton
notification_store = NotificationStore()
