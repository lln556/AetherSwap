import time


class PipelineContext:
    def __init__(self, state, flow_id: str, verbose: bool = False):
        self.state = state
        self.flow_id = flow_id
        self.verbose = verbose
        self.is_policy_current = lambda: True

    def log(self, msg: str, level: str = "info", category: str = "pipeline") -> None:
        self.state.log(msg, level, category=category, flow_id=self.flow_id)

    def debug(self, msg: str, category: str = "pipeline") -> None:
        if self.verbose:
            self.log(msg, level="debug", category=category)

    def set_status(self, stage: str, msg: str, **kwargs) -> None:
        self.state.set_status(stage, msg, **kwargs)

    def is_stop_requested(self) -> bool:
        return self.state.is_stop_requested()

    def buff_auth_block_reason(self) -> tuple[str, str] | None:
        getter = getattr(self.state, "get_status", None)
        status = getter() if callable(getter) else None
        if not isinstance(status, dict):
            return None
        if status.get("buff_auth_expired"):
            return (
                "BUFF_AUTH_EXPIRED",
                "检测到 BUFF 登录已失效，买入流水线已自动停止，请更新 BUFF 会话后手动重启任务",
            )
        if status.get("buff_verification_required"):
            reason = str(status.get("buff_verification_reason") or "").strip()
            detail = f"（{reason}）" if reason else ""
            return (
                "BUFF_VERIFICATION_REQUIRED",
                f"检测到 BUFF 需要安全验证{detail}，买入流水线已自动停止，请在浏览器完成验证并导入会话后手动重启任务",
            )
        return None

    def wait_retry(self, secs: int, *, step: str = "WAITING_RETRY", interrupt_on_policy_change: bool = True) -> bool:
        if self.is_stop_requested():
            self.set_status("stopped", "已停止")
            return True
        blocked = self.buff_auth_block_reason()
        if blocked is not None:
            step_code, reason = blocked
            self.log(reason, "error", category="buff")
            self.set_status("error", step_code)
            return True
        self.set_status("running", step, progress_item=f"Retry in {secs}s")
        for _ in range(secs):
            if self.is_stop_requested():
                self.set_status("stopped", "已停止")
                return True
            blocked = self.buff_auth_block_reason()
            if blocked is not None:
                step_code, reason = blocked
                self.log(reason, "error", category="buff")
                self.set_status("error", step_code)
                return True
            if interrupt_on_policy_change and not self.is_policy_current():
                return False
            time.sleep(1)
        return False
