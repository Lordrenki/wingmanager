import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(level=logging.INFO)

MANAGER_ROLE_IDS = {
    1498007839539466331,
    1497959044185587723,
    1496745530452086844,
}

DB_PATH = Path("missions.db")
SHIPS_PATH = Path("ships.json")


@dataclass
class ActiveShipAssignment:
    message_id: int
    channel_id: int
    guild_id: int
    ship_name: str
    slots: List[str]
    assignments: Dict[str, int]  # slot -> user_id


class MissionStore:
    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mission_counts (
                user_id INTEGER PRIMARY KEY,
                completions INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.commit()

    def add_completion(self, user_id: int) -> None:
        self._conn.execute(
            """
            INSERT INTO mission_counts (user_id, completions)
            VALUES (?, 1)
            ON CONFLICT(user_id)
            DO UPDATE SET completions = completions + 1
            """,
            (user_id,),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class ShipAssignmentBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.active_assignments: Dict[str, ActiveShipAssignment] = {}
        self.mission_store = MissionStore(DB_PATH)
        self.ship_presets = load_ship_presets()

    async def setup_hook(self) -> None:
        self.tree.add_command(shipassignment)
        await self.tree.sync()

    async def close(self) -> None:
        self.mission_store.close()
        await super().close()


def load_ship_presets() -> Dict[str, List[str]]:
    if not SHIPS_PATH.exists():
        raise FileNotFoundError(
            "ships.json not found. Create it with ship preset definitions."
        )
    with SHIPS_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("ships.json must contain an object of ship_name -> slot list")

    normalized: Dict[str, List[str]] = {}
    for ship_name, slots in data.items():
        if not isinstance(slots, list) or not all(isinstance(s, str) for s in slots):
            raise ValueError(f"Ship '{ship_name}' must map to a list of string slots.")
        normalized[ship_name] = slots
    return normalized


def user_is_manager(member: discord.Member) -> bool:
    return any(role.id in MANAGER_ROLE_IDS for role in member.roles)


def render_ship_embed(guild: discord.Guild, assignment: ActiveShipAssignment) -> discord.Embed:
    embed = discord.Embed(
        title=assignment.ship_name,
        description="Use **Pick Assignment** to select your position.",
        color=discord.Color.blurple(),
    )

    lines = []
    for slot in assignment.slots:
        user_id = assignment.assignments.get(slot)
        occupant = guild.get_member(user_id).display_name if user_id else "Available"
        lines.append(f"**{slot}**: {occupant}")

    embed.add_field(name="Assignments", value="\n".join(lines), inline=False)
    return embed


class ShipAssignmentView(discord.ui.View):
    def __init__(self, bot: ShipAssignmentBot, ship_name: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.ship_name = ship_name

    @discord.ui.button(label="Pick Assignment", style=discord.ButtonStyle.primary)
    async def pick_assignment(self, interaction: discord.Interaction, _: discord.ui.Button):
        assignment = self.bot.active_assignments.get(self.ship_name)
        if assignment is None:
            await interaction.response.send_message(
                "This mission is no longer active.", ephemeral=True
            )
            return

        available_slots = [slot for slot in assignment.slots if slot not in assignment.assignments]
        if not available_slots:
            await interaction.response.send_message(
                "No open assignments remain.", ephemeral=True
            )
            return

        view = SlotPickerView(self.bot, self.ship_name, available_slots)
        await interaction.response.send_message("Pick an assignment:", view=view, ephemeral=True)

    @discord.ui.button(label="Unassign Self", style=discord.ButtonStyle.secondary)
    async def unassign_self(self, interaction: discord.Interaction, _: discord.ui.Button):
        assignment = self.bot.active_assignments.get(self.ship_name)
        if assignment is None:
            await interaction.response.send_message(
                "This mission is no longer active.", ephemeral=True
            )
            return

        user_id = interaction.user.id
        removed_slot: Optional[str] = None
        for slot, assigned_user_id in list(assignment.assignments.items()):
            if assigned_user_id == user_id:
                removed_slot = slot
                del assignment.assignments[slot]
                break

        if removed_slot is None:
            await interaction.response.send_message(
                "You do not currently hold an assignment.", ephemeral=True
            )
            return

        await update_assignment_message(self.bot, assignment)
        await interaction.response.send_message(
            f"Removed you from **{removed_slot}**.", ephemeral=True
        )

    @discord.ui.button(label="Manage Ship", style=discord.ButtonStyle.danger)
    async def manage_ship(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not user_is_manager(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to manage this ship.", ephemeral=True
            )
            return

        assignment = self.bot.active_assignments.get(self.ship_name)
        if assignment is None:
            await interaction.response.send_message(
                "This mission is no longer active.", ephemeral=True
            )
            return

        manage_view = ManageShipView(self.bot, self.ship_name)
        await interaction.response.send_message("Manage options:", view=manage_view, ephemeral=True)


class SlotPickerView(discord.ui.View):
    def __init__(self, bot: ShipAssignmentBot, ship_name: str, available_slots: List[str]):
        super().__init__(timeout=120)
        self.bot = bot
        self.ship_name = ship_name

        for slot in available_slots:
            self.add_item(SlotAssignButton(slot, ship_name, bot))


class SlotAssignButton(discord.ui.Button):
    def __init__(self, slot: str, ship_name: str, bot: ShipAssignmentBot):
        super().__init__(label=slot, style=discord.ButtonStyle.success)
        self.slot = slot
        self.ship_name = ship_name
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        assignment = self.bot.active_assignments.get(self.ship_name)
        if assignment is None:
            await interaction.response.send_message("Mission is no longer active.", ephemeral=True)
            return

        user_id = interaction.user.id

        if self.slot in assignment.assignments:
            await interaction.response.send_message("That slot was already taken.", ephemeral=True)
            return

        existing_slot = next(
            (slot for slot, assigned_user in assignment.assignments.items() if assigned_user == user_id),
            None,
        )
        if existing_slot:
            await interaction.response.send_message(
                f"You are already assigned to **{existing_slot}**. Unassign first.",
                ephemeral=True,
            )
            return

        assignment.assignments[self.slot] = user_id
        await update_assignment_message(self.bot, assignment)
        await interaction.response.send_message(
            f"You are now assigned to **{self.slot}**.", ephemeral=True
        )


class ManageShipView(discord.ui.View):
    def __init__(self, bot: ShipAssignmentBot, ship_name: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.ship_name = ship_name

    @discord.ui.button(label="Clear All Assignments", style=discord.ButtonStyle.secondary)
    async def clear_all(self, interaction: discord.Interaction, _: discord.ui.Button):
        assignment = self.bot.active_assignments.get(self.ship_name)
        if assignment is None:
            await interaction.response.send_message("Mission is no longer active.", ephemeral=True)
            return

        assignment.assignments.clear()
        await update_assignment_message(self.bot, assignment)
        await interaction.response.send_message("All assignments were cleared.", ephemeral=True)

    @discord.ui.button(label="Clear Specific Assignment", style=discord.ButtonStyle.secondary)
    async def clear_specific(self, interaction: discord.Interaction, _: discord.ui.Button):
        assignment = self.bot.active_assignments.get(self.ship_name)
        if assignment is None:
            await interaction.response.send_message("Mission is no longer active.", ephemeral=True)
            return

        taken_slots = [slot for slot in assignment.slots if slot in assignment.assignments]
        if not taken_slots:
            await interaction.response.send_message("No taken slots to clear.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Select a slot to clear:",
            view=ClearSpecificView(self.bot, self.ship_name, taken_slots),
            ephemeral=True,
        )

    @discord.ui.button(label="Complete Mission", style=discord.ButtonStyle.success)
    async def complete_mission(self, interaction: discord.Interaction, _: discord.ui.Button):
        assignment = self.bot.active_assignments.get(self.ship_name)
        if assignment is None:
            await interaction.response.send_message("Mission is no longer active.", ephemeral=True)
            return

        unique_users = set(assignment.assignments.values())
        for user_id in unique_users:
            self.bot.mission_store.add_completion(user_id)

        await delete_assignment_message(self.bot, assignment)
        await interaction.response.send_message(
            f"Mission complete. Added +1 completion for {len(unique_users)} user(s).",
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel Mission", style=discord.ButtonStyle.danger)
    async def cancel_mission(self, interaction: discord.Interaction, _: discord.ui.Button):
        assignment = self.bot.active_assignments.get(self.ship_name)
        if assignment is None:
            await interaction.response.send_message("Mission is no longer active.", ephemeral=True)
            return

        await delete_assignment_message(self.bot, assignment)
        await interaction.response.send_message("Mission canceled and tracker deleted.", ephemeral=True)


class ClearSpecificView(discord.ui.View):
    def __init__(self, bot: ShipAssignmentBot, ship_name: str, slots: List[str]):
        super().__init__(timeout=120)
        for slot in slots:
            self.add_item(ClearSpecificButton(bot, ship_name, slot))


class ClearSpecificButton(discord.ui.Button):
    def __init__(self, bot: ShipAssignmentBot, ship_name: str, slot: str):
        super().__init__(label=slot, style=discord.ButtonStyle.secondary)
        self.bot = bot
        self.ship_name = ship_name
        self.slot = slot

    async def callback(self, interaction: discord.Interaction):
        assignment = self.bot.active_assignments.get(self.ship_name)
        if assignment is None:
            await interaction.response.send_message("Mission is no longer active.", ephemeral=True)
            return

        if self.slot not in assignment.assignments:
            await interaction.response.send_message("Slot is already open.", ephemeral=True)
            return

        del assignment.assignments[self.slot]
        await update_assignment_message(self.bot, assignment)
        await interaction.response.send_message(
            f"Cleared **{self.slot}** assignment.", ephemeral=True
        )


async def update_assignment_message(bot: ShipAssignmentBot, assignment: ActiveShipAssignment) -> None:
    guild = bot.get_guild(assignment.guild_id)
    if guild is None:
        return
    channel = guild.get_channel(assignment.channel_id)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return

    message = await channel.fetch_message(assignment.message_id)
    await message.edit(embed=render_ship_embed(guild, assignment), view=ShipAssignmentView(bot, assignment.ship_name))


async def delete_assignment_message(bot: ShipAssignmentBot, assignment: ActiveShipAssignment) -> None:
    bot.active_assignments.pop(assignment.ship_name, None)

    guild = bot.get_guild(assignment.guild_id)
    if guild is None:
        return
    channel = guild.get_channel(assignment.channel_id)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return

    try:
        message = await channel.fetch_message(assignment.message_id)
        await message.delete()
    except discord.NotFound:
        pass


@app_commands.command(name="shipassignment", description="Create a ship assignment tracker")
@app_commands.describe(ship_name="Select the ship preset")
async def shipassignment(interaction: discord.Interaction, ship_name: str):
    bot = interaction.client
    if not isinstance(bot, ShipAssignmentBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    if ship_name not in bot.ship_presets:
        await interaction.response.send_message(
            "Unknown ship preset. Check your configured ship names.", ephemeral=True
        )
        return

    if ship_name in bot.active_assignments:
        await interaction.response.send_message(
            "That ship already has an active assignment tracker.", ephemeral=True
        )
        return

    slots = bot.ship_presets[ship_name]
    assignment = ActiveShipAssignment(
        message_id=0,
        channel_id=interaction.channel_id,
        guild_id=interaction.guild_id or 0,
        ship_name=ship_name,
        slots=slots,
        assignments={},
    )

    embed = render_ship_embed(interaction.guild, assignment) if interaction.guild else discord.Embed(title=ship_name)
    view = ShipAssignmentView(bot, ship_name)
    await interaction.response.send_message(embed=embed, view=view)
    message = await interaction.original_response()

    assignment.message_id = message.id
    bot.active_assignments[ship_name] = assignment


@shipassignment.autocomplete("ship_name")
async def ship_name_autocomplete(interaction: discord.Interaction, current: str):
    bot = interaction.client
    if not isinstance(bot, ShipAssignmentBot):
        return []

    choices = [
        app_commands.Choice(name=ship_name, value=ship_name)
        for ship_name in bot.ship_presets.keys()
        if current.lower() in ship_name.lower()
    ]
    return choices[:25]


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Set DISCORD_TOKEN in environment.")

    bot = ShipAssignmentBot()
    bot.run(token)


if __name__ == "__main__":
    main()
