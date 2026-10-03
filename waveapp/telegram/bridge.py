"""TelegramBridge (SPEC.md §13, Phase 4).

Commands: /status /positions /pnl /pause /resume /stop /kill /help.
Security (SPEC.md §4):
- bot token lives in Keychain (entry `telegram_bot_token`), never in files;
- ONLY the numeric Telegram user ID is accepted — every other sender is
  silently ignored (and logged);
- the four dangerous commands (/pause /resume /stop /kill) execute only after
  the sender replies with a one-time confirmation code (60s expiry);
- live/paper switching is NOT available over Telegram, by design, ever.

Dangerous commands confirm first, then act through the CommandSink protocol —
the seam the engine plugs into without touching this file's security logic
(a fallback sink reports plainly when the engine isn't connected).
"""

from __future__ import annotations

import contextlib
import logging
import secrets as pysecrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from waveapp.broker.base import TradingMode
from waveapp.security import secrets

logger = logging.getLogger("wave.telegram")

KEYCHAIN_BOT_TOKEN = "telegram_bot_token"  # noqa: S105 — Keychain entry NAME
CONFIRM_TTL_SECONDS = 60.0

DANGEROUS_COMMANDS = ("pause", "resume", "stop", "kill")


class _QuietTransientPolling(logging.Filter):
    """Downgrade the Updater's transient-network ERROR tracebacks to INFO
    (2026-09-03: Telegram-side 502 bursts self-heal via the retry loop; the
    ERROR spam woke monitors and can page). Real errors pass untouched."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR and record.exc_info:
            exc = record.exc_info[1]
            try:
                from telegram.error import NetworkError, TimedOut

                if isinstance(exc, (NetworkError, TimedOut)):
                    record.levelno = logging.INFO
                    record.levelname = "INFO"
                    record.exc_info = None
                    record.msg = f"{record.msg} (transient network blip — retrying)"
            except Exception:  # noqa: S110 — never break logging
                pass
        return True


logging.getLogger("telegram.ext.Updater").addFilter(_QuietTransientPolling())
logging.getLogger("telegram.ext.Application").addFilter(_QuietTransientPolling())


class CommandSink(Protocol):
    """What the engine will implement in Phase 5."""

    async def pause(self) -> str: ...
    async def resume(self) -> str: ...
    async def stop(self) -> str: ...
    async def kill(self) -> str: ...


class _EngineNotBuilt:
    """Fallback sink when the engine isn't attached (e.g., broker offline)."""

    async def pause(self) -> str:
        return "Confirmed — but the engine is not connected; nothing to pause."

    async def resume(self) -> str:
        return "Confirmed — but the engine is not connected; nothing to resume."

    async def stop(self) -> str:
        return "Confirmed — but the engine is not connected; nothing to stop."

    async def kill(self) -> str:
        return "Confirmed — but the engine is not connected; nothing to flatten."


@dataclass
class PendingConfirmation:
    command: str
    code: str
    issued_at: float


@dataclass
class ConfirmationGate:
    """One-time confirmation codes for dangerous commands (SPEC.md §13)."""

    ttl: float = CONFIRM_TTL_SECONDS
    time_fn: Callable[[], float] = time.monotonic
    _pending: PendingConfirmation | None = field(default=None, init=False)

    def issue(self, command: str) -> str:
        code = f"{pysecrets.randbelow(1_000_000):06d}"
        self._pending = PendingConfirmation(command, code, self.time_fn())
        return code

    def confirm(self, text: str) -> str | None:
        """Return the confirmed command if `text` is the valid, fresh code."""
        pending = self._pending
        if pending is None:
            return None
        if self.time_fn() - pending.issued_at > self.ttl:
            self._pending = None
            return None
        if text.strip() != pending.code:
            return None
        self._pending = None
        return pending.command

    @property
    def has_pending(self) -> bool:
        return self._pending is not None and self.time_fn() - self._pending.issued_at <= self.ttl


class TelegramBridge:
    """Runs the bot; exposes `push()` for alerts from the rest of Wave."""

    def __init__(
        self,
        allowed_user_id: int,
        adapter_provider: Callable[[], Any | None],
        command_sink: CommandSink | None = None,
        gate: ConfirmationGate | None = None,
        test_entry: Callable[[], Awaitable[str]] | None = None,
        stats_provider: Callable[[], dict] | None = None,
    ) -> None:
        if allowed_user_id <= 0:
            raise ValueError("allowed_user_id must be a positive Telegram user id")
        self.allowed_user_id = allowed_user_id
        self._adapter_provider = adapter_provider
        self._sink: CommandSink = command_sink or _EngineNotBuilt()
        self._gate = gate or ConfirmationGate()
        self._test_entry = test_entry
        self._stats = stats_provider or (lambda: {})
        self._app: Any = None

    # -- security -----------------------------------------------------------

    def is_authorized(self, user_id: int | None) -> bool:
        if user_id != self.allowed_user_id:
            logger.warning("Telegram message from unauthorized user id %s ignored", user_id)
            return False
        return True

    # -- command logic (transport-independent, unit-tested directly) --------

    async def handle_status(self) -> str:
        adapter = self._adapter_provider()
        if adapter is None or not getattr(adapter, "is_connected", False):
            return "🌊 Wave is on, but the broker is not connected yet."
        account = await adapter.get_account(TradingMode.PAPER)
        clock = await adapter.get_clock(TradingMode.PAPER)
        market = "open" if clock.is_open else "closed"
        stats = self._stats() or {}
        today = stats.get("today_pnl")
        today_line = f"\nToday: {today:+,.2f} $" if today is not None else ""
        return (
            f"🌊 Wave is trading (paper money).\n"
            f"Money: ${account.equity:,.2f} (cash ${account.cash:,.2f})\n"
            f"Market: {market}\n"
            f"Positions: {stats.get('positions', 0)} open{today_line}"
        )

    async def handle_positions(self) -> str:
        adapter = self._adapter_provider()
        if adapter is None or not getattr(adapter, "is_connected", False):
            return "Broker not connected."
        positions = await adapter.get_positions(TradingMode.PAPER)
        if not positions:
            return "📊 No open positions right now."
        lines = [f"📊 {len(positions)} open:"]
        for p in positions:
            dot = "🟢" if p.unrealized_pl >= 0 else "🔴"
            side = "" if p.qty >= 0 else " (short)"
            lines.append(
                f"{dot} {p.symbol}{side} {p.unrealized_pl:+,.2f} $ "
                f"({abs(p.qty):g} @ ${p.avg_entry_price:.2f})"
            )
        return "\n".join(lines)

    async def handle_pnl(self) -> str:
        adapter = self._adapter_provider()
        if adapter is None or not getattr(adapter, "is_connected", False):
            return "Broker not connected."
        positions = await adapter.get_positions(TradingMode.PAPER)
        unrealized = sum(p.unrealized_pl for p in positions)
        account = await adapter.get_account(TradingMode.PAPER)
        stats = self._stats() or {}
        lines = [f"💰 Money: ${account.equity:,.2f}"]
        if stats.get("today_pnl") is not None:
            lines.append(
                f"Today: {stats['today_pnl']:+,.2f} $ "
                f"({stats.get('today_trades', 0)} trades, {stats.get('today_wins', 0)} won)"
            )
        if stats.get("week_pnl") is not None:
            lines.append(f"This week: {stats['week_pnl']:+,.2f} $")
        if positions:
            lines.append(f"Open right now: {unrealized:+,.2f} $ on {len(positions)} position(s)")
        return "\n".join(lines)

    async def handle_today(self) -> str:
        stats = self._stats() or {}
        trades = stats.get("today_list") or []
        if not trades:
            return "No closed trades yet today."
        lines = []
        for symbol, pnl in trades:
            dot = "🟢" if pnl > 0 else ("⚪" if pnl == 0 else "🔴")
            verdict = "WON" if pnl > 0 else ("even" if pnl == 0 else "LOST")
            lines.append(f"{dot} {symbol}: {verdict} {pnl:+,.2f} $")
        total = sum(p for _s, p in trades)
        lines.append(f"— Today so far: {total:+,.2f} $ ({len(trades)} trades)")
        return "\n".join(lines)

    async def handle_week(self) -> str:
        stats = self._stats() or {}
        days = stats.get("week_days") or []
        if not days:
            return "No trades this week yet."
        lines = ["🗓 This week:"]
        for day, pnl, count in days:
            dot = "🟢" if pnl >= 0 else "🔴"
            lines.append(f"{dot} {day}: {pnl:+,.2f} $ ({count} trades)")
        lines.append(f"— Week total: {stats.get('week_pnl', 0):+,.2f} $")
        best = stats.get("week_best")
        worst = stats.get("week_worst")
        if best:
            lines.append(f"Best trade: {best[0]} {best[1]:+,.2f} $")
        if worst:
            lines.append(f"Worst trade: {worst[0]} {worst[1]:+,.2f} $")
        return "\n".join(lines)

    async def handle_scanner(self) -> str:
        stats = self._stats() or {}
        scan = stats.get("scanner") or {}
        if not scan:
            return "🔎 The scanner has not run yet."
        top = ", ".join(scan.get("top") or []) or "—"
        return (
            f"🔎 Scanner:\n"
            f"Last scan: {scan.get('at', '—')}\n"
            f"Checked {scan.get('candidates', 0)} stocks, "
            f"{scan.get('accepted', 0)} passed the quality gate.\n"
            f"Top right now: {top}"
        )

    _DANGER_NOTES = {
        "pause": "This stops NEW buys. Open positions stay protected and keep managing.",
        "resume": "This allows new buys again.",
        "stop": "No new buys; Wave rests after every position closes.",
        "kill": "This sells EVERYTHING immediately and halts trading.",
        "test": "This buys 1 test share (paper) with a safety stop 1% below.",
    }

    async def handle_dangerous(self, command: str) -> str:
        code = self._gate.issue(command)
        note = self._DANGER_NOTES.get(command, "")
        return (
            f"⚠️ To confirm /{command}, reply with this code in the next "
            f"{int(self._gate.ttl)} seconds: {code}\n{note}"
        )

    async def handle_text(self, text: str) -> str | None:
        """A plain text message: only meaningful as a confirmation code."""
        command = self._gate.confirm(text)
        if command is None:
            return None
        logger.warning("Telegram-confirmed command executing: /%s", command)
        if command == "test":
            if self._test_entry is None:
                return "test entry not available"
            return await self._test_entry()
        action: Callable[[], Awaitable[str]] = getattr(self._sink, command)
        return await action()

    # (command, menu description, help detail)
    COMMANDS = (
        ("status", "How is Wave doing", "money, market open/closed, open positions, today"),
        ("positions", "What Wave is holding", "one line per position, green/red"),
        ("pnl", "The money picture", "account, today, this week, open profit"),
        ("today", "Today's closed trades", "every trade with WIN/LOST and the day total"),
        ("week", "This week day by day", "daily results, week total, best and worst trade"),
        ("scanner", "What the scanner sees", "last scan time, stocks checked, top candidates"),
        ("pause", "Stop new buys", "⚠️ needs code reply. Positions stay protected"),
        ("resume", "Allow buys again", "⚠️ needs code reply"),
        ("stop", "Finish and rest", "⚠️ needs code reply. Rests after all positions close"),
        ("kill", "SELL EVERYTHING NOW", "⚠️ needs code reply. Immediate — then halted"),
        ("test", "Tiny test trade", "⚠️ needs code reply. 1 paper share, stop 1% below"),
        ("help", "This list", "shows all commands"),
    )

    HELP = (
        "🌊 Wave commands\n\n"
        + "\n".join(f"/{name} — {menu}\n    {detail}" for name, menu, detail in COMMANDS)
        + "\n\n⚠️ marked commands send back a one-time 6-digit code — "
        "reply with the code within 60 seconds to run them. Wrong or late code = nothing happens.\n"
        "Switching to LIVE money is never possible from Telegram, by design."
    )

    # -- transport (python-telegram-bot) ------------------------------------

    @staticmethod
    def token_from_keychain() -> str | None:
        return secrets.get_secret(KEYCHAIN_BOT_TOKEN)

    def build_application(self, token: str) -> Any:
        from telegram.ext import (
            ApplicationBuilder,
            CommandHandler,
            MessageHandler,
            filters,
        )

        app = ApplicationBuilder().token(token).build()

        def guard(handler: Callable[[], Awaitable[str]]):
            async def wrapped(update: Any, context: Any) -> None:
                user = update.effective_user
                if user is None or not self.is_authorized(user.id):
                    return
                reply = await handler()
                if reply:
                    await update.effective_message.reply_text(reply)

            return wrapped

        app.add_handler(CommandHandler("status", guard(self.handle_status)))
        app.add_handler(CommandHandler("positions", guard(self.handle_positions)))
        app.add_handler(CommandHandler("pnl", guard(self.handle_pnl)))
        app.add_handler(CommandHandler("today", guard(self.handle_today)))
        app.add_handler(CommandHandler("week", guard(self.handle_week)))
        app.add_handler(CommandHandler("scanner", guard(self.handle_scanner)))
        app.add_handler(CommandHandler("help", guard(lambda: _async_value(self.HELP))))
        for command in (*DANGEROUS_COMMANDS, "test"):
            app.add_handler(
                CommandHandler(
                    command,
                    guard(lambda c=command: self.handle_dangerous(c)),
                )
            )

        async def on_text(update: Any, context: Any) -> None:
            user = update.effective_user
            if user is None or not self.is_authorized(user.id):
                return
            text = update.effective_message.text or ""
            reply = await self.handle_text(text)
            if reply:
                await update.effective_message.reply_text(reply)
            elif text.strip():
                # a plain text that isn't a confirmation code is a note to
                # self — log it verbatim and acknowledge so the phone side
                # knows it landed.
                logger.info("Telegram note: %s", text)
                await update.effective_message.reply_text("📨 noted")

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

        async def on_unknown_command(update: Any, context: Any) -> None:
            user = update.effective_user
            if user is None or not self.is_authorized(user.id):
                return
            # 2026-09-03: an unknown command always answers — silence reads
            # as breakage from the phone side.
            await update.effective_message.reply_text(
                "🤷 Unknown command on this build — /help lists what I know."
            )

        app.add_handler(MessageHandler(filters.COMMAND, on_unknown_command))

        async def on_error(update: Any, context: Any) -> None:
            # 2026-09-03: Telegram's servers
            # 502 in short bursts; the library's retry loop self-heals, but
            # the default handler screamed ERROR tracebacks (14 in the log,
            # and ERROR-level lines can page). Transient network errors are
            # an INFO heartbeat now; anything else stays loud.
            from telegram.error import NetworkError, TimedOut

            err = getattr(context, "error", None)
            if isinstance(err, (NetworkError, TimedOut)):
                logger.info("Telegram transient network blip (%s) — retrying", err)
                return
            logger.error("Telegram handler error: %s", err, exc_info=err)

        app.add_error_handler(on_error)
        self._app = app
        return app

    async def start(self) -> None:
        token = self.token_from_keychain()
        if not token:
            raise RuntimeError(
                f"Keychain entry '{KEYCHAIN_BOT_TOKEN}' missing — store the bot token first"
            )
        app = self.build_application(token)
        await app.initialize()
        # register the command menu so Telegram shows them under the “/” button
        from telegram import BotCommand

        with contextlib.suppress(Exception):
            await app.bot.set_my_commands(
                [BotCommand(name, menu) for name, menu, _ in self.COMMANDS]
            )
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bridge polling (authorized user: %d)", self.allowed_user_id)

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        finally:
            self._app = None
        logger.info("Telegram bridge stopped")

    @property
    def is_running(self) -> bool:
        return self._app is not None

    async def push(self, text: str) -> None:
        """Push alert to the phone (fills, halts, risk events — later phases)."""
        if self._app is None:
            return
        try:
            await self._app.bot.send_message(chat_id=self.allowed_user_id, text=text)
        except Exception:
            logger.exception("Telegram push failed")


async def _async_value(value: str) -> str:
    return value
