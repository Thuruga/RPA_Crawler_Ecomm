from fastapi import APIRouter
from services.notification_service import notification_store

router = APIRouter(prefix="/system", tags=["Sistema de Notificações"])

@router.get("/status")
async def get_system_status():
    """
    Retorna as notificações não lidas e os horários das últimas varreduras.
    """
    return {
        "notifications": notification_store.get_all(),
        "unread_count": len(notification_store.get_unread()),
        "last_category_run": notification_store.last_category_run,
        "last_price_run": notification_store.last_price_run
    }

@router.post("/notifications/{notif_id}/read")
async def mark_notification_read(notif_id: str):
    """
    Marca uma notificação como lida.
    """
    notification_store.mark_as_read(notif_id)
    return {"status": "ok"}

@router.post("/notifications/read-all")
async def mark_all_notifications_read():
    """
    Marca todas as notificações como lidas.
    """
    notification_store.mark_all_as_read()
    return {"status": "ok"}
