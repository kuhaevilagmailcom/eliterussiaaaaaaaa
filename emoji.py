from __future__ import annotations

from aiogram import Bot


class EmojiBank:
    def __init__(self, pack_names: tuple[str, ...]):
        self.pack_names = pack_names
        self.ids: list[str] = []

    async def load(self, bot: Bot) -> None:
        loaded: list[str] = []
        for name in self.pack_names:
            try:
                sticker_set = await bot.get_sticker_set(name)
            except Exception:
                continue
            for sticker in sticker_set.stickers:
                custom_id = getattr(sticker, "custom_emoji_id", None)
                if custom_id:
                    loaded.append(custom_id)
        self.ids = loaded

    def icon(self, index: int, fallback: str) -> str:
        if not self.ids:
            return fallback
        custom_id = self.ids[index % len(self.ids)]
        return f'<tg-emoji emoji-id="{custom_id}">{fallback}</tg-emoji>'
