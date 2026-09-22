from __future__ import annotations

from aiogram import Bot


class EmojiBank:
    def __init__(self, pack_names: tuple[str, ...]):
        self.pack_names = pack_names
        self.pack_ids: dict[str, list[str]] = {}
        self.ids: list[str] = []

    async def load(self, bot: Bot) -> None:
        self.pack_ids = {}
        self.ids = []

        for name in self.pack_names:
            loaded: list[str] = []
            try:
                sticker_set = await bot.get_sticker_set(name)
            except Exception:
                self.pack_ids[name] = []
                continue

            for sticker in sticker_set.stickers:
                custom_id = getattr(sticker, "custom_emoji_id", None)
                if custom_id:
                    loaded.append(custom_id)
                    self.ids.append(custom_id)

            self.pack_ids[name] = loaded

    def icon(
        self,
        index: int,
        fallback: str = "⭐",
        *,
        pack: str | None = None,
    ) -> str:
        pool = self.pack_ids.get(pack, []) if pack else self.ids
        if not pool:
            return fallback

        custom_id = pool[index % len(pool)]

        # Telegram requires valid entity text inside <tg-emoji>.
        # A normal Unicode emoji is used only as the hidden fallback;
        # the visible emoji comes from custom_emoji_id.
        return f'<tg-emoji emoji-id="{custom_id}">⭐</tg-emoji>'
