"""Transport receipts, independent of model wording or predicted device state."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Delivery:
    status: str
    sent: bool = False
    reason: str = ""
    superseded: bool = False  # Internal only: a newer stop already owns the state.

    def __bool__(self):
        return self.status in {"confirmed", "sent", "unchanged"}


def delivery(value) -> Delivery:
    """Existing BLE/fake adapters still expose a boolean write result."""
    if isinstance(value, Delivery):
        return value
    if value is True:
        return Delivery("sent", True, "命令已发送")
    return Delivery("failed", False, "设备命令发送失败")


def combine(results) -> Delivery:
    values = [delivery(value) for value in results]
    if not values:
        return Delivery("unchanged", False, "当前状态无需重复发送")
    sent = any(value.sent for value in values)
    for status in ("unconfirmed", "failed"):
        failures = [value for value in values if value.status == status]
        if failures:
            # A multi-frame command can have partially executed. Never retry it.
            reason = failures[0].reason
            if sent:
                reason += "；部分命令可能已经生效，请检查设备状态"
            return Delivery(status, sent, reason)
    return Delivery("confirmed" if all(value.status == "confirmed" for value in values) else "sent",
                    sent, "设备已确认" if all(value.status == "confirmed" for value in values) else "命令已发送")
