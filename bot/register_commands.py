"""Register the bot's slash commands with Discord. Run once, and again after changing COMMANDS.

Usage:
    DISCORD_APPLICATION_ID=... DISCORD_BOT_TOKEN=... python bot/register_commands.py

Commands are registered globally, so they appear in every server the bot is in.
"""

import os

import requests

COMMANDS = [
    {
        "name": "market",
        "description": "当前美股行情总结：指数、板块、利率、商品、加密",
        "type": 1,  # slash command
    },
    {
        "name": "dip",
        "description": "抄底回测：某只股票上市以来的单日大跌，以及之后 1 / 20 / 60 个交易日的表现",
        "type": 1,
        "options": [
            {
                "name": "symbol",
                "description": "美股代码，例如 GOOGL",
                "type": 3,  # string
                "required": True,
            },
            {
                "name": "threshold",
                "description": "单日跌幅阈值（%），不填默认 8%",
                "type": 10,  # number
                "required": False,
                "min_value": 1,
                "max_value": 50,
            },
        ],
    },
]


def main():
    application_id = os.environ["DISCORD_APPLICATION_ID"]
    token = os.environ["DISCORD_BOT_TOKEN"]

    response = requests.put(
        f"https://discord.com/api/v10/applications/{application_id}/commands",
        headers={"Authorization": f"Bot {token}"},
        json=COMMANDS,
        timeout=30,
    )
    if response.status_code >= 400:
        raise SystemExit(f"Discord refused the commands: HTTP {response.status_code} {response.text[:300]}")
    print("Registered:", ", ".join(f"/{command['name']}" for command in response.json()))


if __name__ == "__main__":
    main()
