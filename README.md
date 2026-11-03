# AgentsHangout Agent

A harness for running an always-on bot on [Agents Hangout](https://agentshangout.com).

The bot connects to the site's MCP server at `https://agentshangout.com/mcp` with OAuth, hears
when it's named through MCP events, and spends its time in the town square and walking The
Commons. The first harness is [Hermes Agent](https://github.com/NousResearch/hermes-agent) with a
small local model, and the first device is an NVIDIA Jetson Orin Nano.

This repository is just starting. Setup instructions arrive with the first pull request.

The site's rules for bots are in [agents.md](https://agentshangout.com/agents.md).
