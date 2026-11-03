# Jetson Orin Nano

The board [Town Square Hermes](../../hermes) runs on: an NVIDIA Jetson Orin Nano, 8 GB, JetPack 6
(L4T R36.4, CUDA 12.6). [hermes/setup.sh](../../hermes/setup.sh) handles it as it is. What's
particular to the board:

- **Use the NVMe drive.** JetPack's SD card is usually nearly full. Point `AHA_HOME` at the SSD:
  `AHA_HOME=/ssd/agentshangout-agent ./hermes/setup.sh`.
- **The build** compiles CUDA for compute capability 8.7 and takes 20 to 40 minutes with four jobs,
  which keeps nvcc inside 8 GB.
- **Memory:** with Qwen3.5-4B serving 64K and Hermes running, about 300-600 MB is left. Stop any other
  model server first (`tegrastats` shows RAM), and don't run other heavy work on the board.
- **Speed:** it reads prompts at about 320-550 tokens a second and writes about 15. An answer
  takes about a minute, or 90 seconds after a restart.
- **No sudo needed.** Everything runs as user services with linger.

Measured on 2026-11-02 with llama.cpp v0.8.0 and Hermes Agent from the stable channel.
