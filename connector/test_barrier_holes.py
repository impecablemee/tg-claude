#!/usr/bin/env python3
"""Проверяет барьер там, где он раньше пропускал. Без сети.

Барьер был устроен как список ЗАПРЕЩЁННЫХ приставок. Такой список закрывает то,
о чём вспомнили, и молча пропускает остальное — а в API Telegram остального
много. Здесь перечислено ровно то, что сквозь него проходило, поимённо, чтобы
обратная правка была видна как падение теста, а не как рассуждение.

Четыре свойства:
  1. одиннадцать бывших дыр закрыты — среди них выход из аккаунта человека,
     сброс всех его сессий и звонок (звонки в задаче прямо вне скоупа);
  2. всё, что заказчик назвал вне скоупа, отклоняется поимённо;
  3. барьер стоит и на `_call`: сама библиотека местами зовёт его напрямую,
     минуя `__call__`, и барьер на одной точке обходится изнутри;
  4. чтения живого режима и скачивание из чужого дата-центра НЕ перекрыты —
     иначе живой режим умрёт молча, и виноват будет не он.

    python3 test_barrier_holes.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import readonly                                                       # noqa: E402
from telethon import TelegramClient                                   # noqa: E402
from telethon.tl import functions as f                                # noqa: E402
from telethon.tl.types import InputPeerSelf as S                      # noqa: E402

FORMER_HOLES = [
    ("выход из аккаунта",        f.auth.LogOutRequest()),
    ("сброс всех сессий",        f.auth.ResetAuthorizationsRequest()),
    ("сброс веб-сессий",         f.account.ResetWebAuthorizationsRequest()),
    ("звонок",                   f.phone.RequestCallRequest(user_id=S(), random_id=1,
                                                            g_a_hash=b"", protocol=None)),
    ("решение по заявке в чат",  f.messages.HideChatJoinRequestRequest(peer=S(), user_id=S())),
    ("установка стикеров",       f.messages.InstallStickerSetRequest(stickerset=None,
                                                                     archived=False)),
    ("удаление стикеров",        f.messages.UninstallStickerSetRequest(stickerset=None)),
    ("очистка недавних",         f.messages.ClearRecentStickersRequest()),
    ("миграция чата",            f.messages.MigrateChatRequest(chat_id=1)),
    ("снятие всех закреплений",  f.messages.UnpinAllMessagesRequest(peer=S())),
    ("сброс сохранённых контактов", f.contacts.ResetSavedRequest()),
]

OUT_OF_SCOPE = [
    ("отправка",            f.messages.SendMessageRequest(peer=S(), message="x", random_id=1)),
    ("реакция",             f.messages.SendReactionRequest(peer=S(), msg_id=1)),
    ("вступление в чат",    f.channels.JoinChannelRequest(channel=S())),
    ("отметка прочитанным", f.messages.ReadHistoryRequest(peer=S(), max_id=0)),
    ("правка сообщения",    f.messages.EditMessageRequest(peer=S(), id=1, message="x")),
    ("удаление сообщений",  f.messages.DeleteMessagesRequest(id=[1])),
]

MUST_PASS = [
    ("состояние обновлений", f.updates.GetStateRequest()),
    ("разница обновлений",   f.updates.GetDifferenceRequest(pts=1, date=None, qts=1)),
    ("состав папки",         f.messages.GetDialogFiltersRequest()),
    ("история чата",         f.messages.GetHistoryRequest(peer=S(), offset_id=0, offset_date=None,
                                                          add_offset=0, limit=1, max_id=0,
                                                          min_id=0, hash=0)),
    ("скачивание файла",     f.upload.GetFileRequest(location=None, offset=0, limit=1)),
    ("авторизация на чужом DC", f.auth.ImportAuthorizationRequest(id=1, bytes=b"")),
    ("шаг CDN",              f.upload.ReuploadCdnFileRequest(file_token=b"", request_token=b"")),
]


def blocked(req) -> bool:
    try:
        readonly.check(req)
        return False
    except readonly.ReadOnlyViolation:
        return True


async def bypass_closed() -> bool:
    """`_call` — внутренний путь библиотеки. Барьер обязан стоять и на нём."""
    readonly.install()
    client = TelegramClient.__new__(TelegramClient)         # без сети и без сессии
    try:
        await client._call(None, f.messages.SendMessageRequest(peer=S(), message="x",
                                                               random_id=1))
    except readonly.ReadOnlyViolation:
        return True
    except Exception as exc:                                          # noqa: BLE001
        print("     дошло до библиотеки: %s" % type(exc).__name__)
        return False
    return False


def main() -> int:
    ok = True

    left = [n for n, r in FORMER_HOLES if not blocked(r)]
    print("1) бывшие дыры: закрыто %d из %d%s"
          % (len(FORMER_HOLES) - len(left), len(FORMER_HOLES),
             "" if not left else " · ОСТАЛИСЬ: %s" % left))
    ok &= not left

    left = [n for n, r in OUT_OF_SCOPE if not blocked(r)]
    print("2) вне скоупа по задаче: отклонено %d из %d%s"
          % (len(OUT_OF_SCOPE) - len(left), len(OUT_OF_SCOPE),
             "" if not left else " · ПРОХОДЯТ: %s" % left))
    ok &= not left

    closed = asyncio.run(bypass_closed())
    print("3) обход через _call: %s" % ("закрыт" if closed else "ОТКРЫТ"))
    ok &= closed

    over = [n for n, r in MUST_PASS if blocked(r)]
    print("4) нужное чтение не перекрыто: прошло %d из %d%s"
          % (len(MUST_PASS) - len(over), len(MUST_PASS),
             "" if not over else " · ПЕРЕКРЫТО ЛИШНЕЕ: %s" % over))
    ok &= not over

    print("\nBARRIER HOLES: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
