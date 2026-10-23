import asyncio
from timetable_bot.config import load_config
from timetable_bot.discord_ui import TimetableCog
import bot as botmod

config = load_config(require_discord=False)
b = botmod.TimetableBot(config)

async def main():
    cog = TimetableCog(b)
    print("cog app commands:", [c.name for c in cog.__cog_app_commands__])
    await b.add_cog(cog)
    print("tree commands   :", [c.name for c in b.tree.get_commands()])
    print("tree size       :", len(b.tree.get_commands()))

asyncio.run(main())
