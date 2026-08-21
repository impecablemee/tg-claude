"""The read-only barrier. Import this before any Telegram client is used.

Phase 1 promises the client one thing above all others: *this system cannot send.*
A promise in a README is not a promise — the code that would break it is one
`await client.send_message(...)` away, and nobody reviews every commit.

So the ban lives on the RESULT, not on the intention: every MTProto request this
process makes passes through `check()`, and anything that writes to Telegram
raises before it reaches the socket. A new file, a new author, an agent writing
code at 2am — all of them hit the same door, because the door is under them, not
beside them.

Two layers, because either one alone is defeatable:
  1. runtime — this module. Patched onto `TelegramClient` itself (on `__call__`
     AND on the internal `_call`, which the library uses directly for the
     data-centre switch and the CDN step — a barrier on one point is bypassed
     from inside the library), so it also covers the high-level helpers
     (`client.send_message`, `client.forward_messages` …).
  2. static — `guard_readonly.py`: a connector file that opens a Telegram client
     without installing this barrier fails CI.

ALLOW what is listed; refuse everything else. The first version of this file did
the opposite — a list of forbidden prefixes — and a deny-list closes what someone
thought of. Measured on that version: `auth.LogOut` (logs the person out of
Telegram), `auth.ResetAuthorizations`, `phone.RequestCall` (calls are explicitly
out of scope), `messages.MigrateChat`, `messages.UnpinAllMessages`,
`contacts.ResetSaved` and five more went straight through. None of them would
have been noticed until someone called one.

An allow-list errs the other way: an unfamiliar READ gets refused, and that is
visible immediately — as a refusal, not as a consequence. `test_barrier_holes.py`
names all eleven, so reverting this is a failing test rather than an argument.

Prefixes, not exact names, on both sides. `messages.SendMessage`,
`messages.SendMedia`, `messages.SendReaction` and whatever Telegram adds next all
start with `Send`; a list of exact class names would silently let the next one
through.

Two deliberate design notes:

* **`Read*` is a write.** `messages.ReadHistory` marks a salesperson's chats as
  read. That is a visible side effect in their pocket and the fastest possible
  way to lose their consent. It is blocked.
* **`tg_login.py` does NOT install this barrier**, and must not: QR device-link
  needs `auth.ImportLoginToken`, which this correctly refuses. Logging in is a
  one-time, human-present act on the account owner's own device. It is exempted
  by name in `guard_readonly.py`, with this reason, and nowhere else.
"""
from __future__ import annotations

import logging

log = logging.getLogger("connector.readonly")

# РАЗРЕШЕНО ТО, ЧТО ПЕРЕЧИСЛЕНО. Всё остальное отклоняется.
#
# Так было написано в шапке с самого начала — «Reads are Get*/Search*/...», — но
# сделано было наоборот: список ЗАПРЕЩЁННЫХ приставок. Разница не стилистическая.
# Запрещающий список закрывает то, о чём вспомнили, и молча пропускает всё
# остальное; в API Telegram остального много. Замерено на этом файле: сквозь него
# проходили `auth.LogOut` (выход из аккаунта человека), `auth.ResetAuthorizations`,
# `phone.RequestCall` (звонок — то, что в задаче прямо вне скоупа),
# `messages.MigrateChat`, `messages.UnpinAllMessages`, `contacts.ResetSaved` и ещё
# пять. Ни одна не была бы замечена, пока её кто-нибудь не позовёт.
#
# Разрешающий список ошибается в другую сторону: незнакомое чтение будет
# отклонено, и это видно сразу — по отказу, а не по последствиям.
READ_PREFIXES = (
    "Get",         # GetHistory, GetDialogFilters, GetFile, GetUsers, GetState, GetDifference…
    "Search",      # SearchGlobal, SearchMessages
    "Resolve",     # ResolveUsername
    "Check",       # CheckUsername
    "Init",        # InitConnection — обёртка, содержимое проверяется отдельно
    "Ping",        # Ping, PingDelayDisconnect
    "Invoke",      # InvokeWithLayer / InvokeWithoutUpdates — тоже обёртки
    "Export",      # ExportLoginToken (QR-вход), ExportChatInvite не нужен и не зовётся
    "Transcribe",  # TranscribeAudio — расшифровка голосового, чтение
)

# Второй слой, избыточный по замыслу: даже если приставка чтения когда-нибудь
# совпадёт с пишущим методом, он назовёт его по имени. Список остаётся не как
# защита, а как объяснение отказа.
FORBIDDEN_PREFIXES = (
    "Send",        # SendMessage, SendMedia, SendReaction, SendMultiMedia, ...
    "Edit",        # EditMessage, EditChatTitle, ...
    "Delete",      # DeleteMessages, DeleteHistory, DeleteChat, ...
    "Forward",     # ForwardMessages
    "Join", "Leave", "Import", "Invite", "Add", "Remove",
    "Set",         # SetTyping, SetHistoryTTL, ...
    "Toggle", "Update", "Create", "Report", "Block", "Unblock",
    "Save",        # SaveDraft, SaveGif, ...
    "Register", "Reorder", "Start", "Upload", "Discard", "Accept",
    "Read",        # ReadHistory / ReadMentions — see the module docstring.
    "Mark",        # MarkDialogUnread
)

# Requests we explicitly allow despite matching a prefix. Every entry needs a
# reason, and the list stays short — it is the crack in the door.
ALLOWED_EXACT: dict[str, str] = {
    # Обе — части СКАЧИВАНИЯ, вопреки названиям, и обе нужны, только когда файл
    # лежит не в том дата-центре, где сидит сессия. Без них большое вложение из
    # чужого DC не скачается — а вложения стоят отдельным пунктом в задаче.
    "ImportAuthorization": "перенос уже выданной авторизации на другой DC — так "
                           "Telethon качает файл из чужого дата-центра; ничего не "
                           "меняет, читать без неё нельзя",
    "ReuploadCdnFile": "шаг протокола CDN при скачивании большого файла: сервер "
                       "просит перезалить кусок в свой кэш. Пишет в CDN, не в "
                       "переписку",
}


class ReadOnlyViolation(RuntimeError):
    """Raised instead of performing a Telegram write."""


def _name(request) -> str:
    return type(request).__name__.removesuffix("Request")


def check(request) -> None:
    """Raise unless `request` — and everything nested inside it — only reads."""
    n = _name(request)
    if n in ALLOWED_EXACT:
        return
    named_write = next((p for p in FORBIDDEN_PREFIXES if n.startswith(p)), None)
    if named_write or not n.startswith(READ_PREFIXES):
        hint = ""
        if n == "UpdateStatus":
            hint = (" — this is Telethon's keepalive marking the account "
                    "online. Pass receive_updates=False when constructing "
                    "the client; do NOT add an exception here.")
        elif not named_write:
            hint = (" — not a known read either. This barrier allows only "
                    f"{'/'.join(p + '*' for p in READ_PREFIXES)}; if this really is a "
                    "read, add its prefix to READ_PREFIXES with a reason.")
        raise ReadOnlyViolation(
            f"blocked write to Telegram: {n} — this connector is read-only "
            f"(connector/readonly.py).{hint} If a later phase needs this, it "
            f"gets its own service and its own signed approval, not an "
            f"exception in this file."
        )
    # InvokeWithLayer(InitConnection(query=...)) nests the real request one or
    # two levels down. Checking only the outer wrapper would wave it straight
    # through, which is exactly how a barrier ends up decorative.
    inner = getattr(request, "query", None)
    if inner is not None and hasattr(inner, "CONSTRUCTOR_ID"):
        check(inner)


def install(client=None) -> None:
    """Patch the client so every request is checked. Idempotent.

    ДВЕ ТОЧКИ, а не одна. `TelegramClient.__call__` — то, чем пользуется наш код и
    все высокоуровневые методы вроде `send_message`. Но сама библиотека местами
    зовёт `client._call(sender, request)` НАПРЯМУЮ, минуя `__call__`: так идут
    смена дата-центра и шаг CDN при скачивании. Значит, барьер, стоящий только на
    `__call__`, обходится изнутри библиотеки — не злым умыслом, а устройством.
    Проверено на telethon 1.43.0: `auth.ImportAuthorization` и
    `upload.ReuploadCdnFile` уходили именно так.

    Патчится КЛАСС, не экземпляр: Python ищет `__call__` на типе, поэтому атрибут
    на экземпляре не сработал бы никогда и барьер остался бы комментарием.
    `client` принимается и игнорируется, чтобы вызовы читались естественно.
    """
    from telethon import TelegramClient

    if getattr(TelegramClient, "_readonly_installed", False):
        return

    def _wrap(original):
        async def guarded(self, *args, **kwargs):
            # У `__call__` запрос первым аргументом, у `_call` — вторым (после
            # sender). Проверяем оба, чтобы обёртка не зависела от сигнатуры.
            for a in args[:2]:
                for r in (a if isinstance(a, (list, tuple)) else [a]):
                    if hasattr(r, "CONSTRUCTOR_ID"):
                        check(r)
            return await original(self, *args, **kwargs)
        return guarded

    TelegramClient.__call__ = _wrap(TelegramClient.__call__)
    inner = getattr(TelegramClient, "_call", None)
    if inner is not None:
        TelegramClient._call = _wrap(inner)
    TelegramClient._readonly_installed = True
    log.info("read-only barrier installed (%d read prefixes, %d named writes, %d exceptions%s)",
             len(READ_PREFIXES), len(FORBIDDEN_PREFIXES), len(ALLOWED_EXACT),
             "" if inner is not None else "; NOTE: _call not found in this telethon")


def selftest() -> None:
    """Proof the barrier is not decorative. Run by guard_readonly.py and by CI."""
    from telethon.tl import functions
    from telethon.tl.types import InputPeerSelf

    blocked = 0
    for r in (
        functions.messages.SendMessageRequest(peer=InputPeerSelf(), message="x", random_id=1),
        functions.messages.ReadHistoryRequest(peer=InputPeerSelf(), max_id=0),
        functions.channels.JoinChannelRequest(channel=InputPeerSelf()),
        functions.messages.ForwardMessagesRequest(from_peer=InputPeerSelf(), id=[1],
                                                  random_id=[1], to_peer=InputPeerSelf()),
        # nested — the wrapper is harmless, the payload is not
        functions.InvokeWithLayerRequest(
            layer=1,
            query=functions.messages.SendMessageRequest(
                peer=InputPeerSelf(), message="x", random_id=1)),
        # Всё ниже проходило сквозь запрещающий список. Каждая строка — не
        # гипотеза: проверено на этом файле до того, как он стал разрешающим.
        functions.auth.LogOutRequest(),                    # выход из аккаунта человека
        functions.auth.ResetAuthorizationsRequest(),       # сброс всех его сессий
        functions.phone.RequestCallRequest(user_id=InputPeerSelf(), random_id=1,
                                           g_a_hash=b"", protocol=None),  # звонок
        functions.messages.MigrateChatRequest(chat_id=1),
        functions.messages.UnpinAllMessagesRequest(peer=InputPeerSelf()),
        functions.messages.InstallStickerSetRequest(stickerset=None, archived=False),
        functions.contacts.ResetSavedRequest(),
    ):
        try:
            check(r)
        except ReadOnlyViolation:
            blocked += 1
        else:
            raise AssertionError(f"barrier did NOT block {_name(r)}")

    allowed = 0
    for r in (
        functions.messages.GetDialogFiltersRequest(),
        functions.messages.GetHistoryRequest(peer=InputPeerSelf(), offset_id=0, offset_date=None,
                                             add_offset=0, limit=1, max_id=0, min_id=0, hash=0),
        functions.upload.GetFileRequest(location=None, offset=0, limit=1),
        # То, что Telethon шлёт САМ в живом режиме: если разрешающий список это
        # заблокирует, живой режим умрёт молча, и виноват будет не он.
        functions.updates.GetStateRequest(),
        functions.updates.GetDifferenceRequest(pts=1, date=None, qts=1),
        functions.updates.GetChannelDifferenceRequest(channel=InputPeerSelf(), filter=None,
                                                      pts=1, limit=1),
        functions.users.GetUsersRequest(id=[InputPeerSelf()]),
        functions.help.GetConfigRequest(),
        functions.PingRequest(ping_id=1),
        functions.InvokeWithLayerRequest(layer=1, query=functions.help.GetConfigRequest()),
    ):
        check(r)  # raises if we over-blocked a read
        allowed += 1

    print(f"readonly selftest: OK ({blocked} writes blocked, {allowed} reads allowed)")


if __name__ == "__main__":
    selftest()
