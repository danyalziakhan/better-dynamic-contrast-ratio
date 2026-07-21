# Better Dynamic Contrast Ratio (DCR) - [PROOF OF CONCEPT]

Software implementation of the **Dynamic Contrast Ratio (DCR)** technology (sometimes also known as **Advanced Contrast Ratio (ACR)**, **Smart Contrast Ratio (SCR)** and other similar names) that is found in many SDR monitors but often lacks support for any kind of manual adjustments (such as the minimum and maximum allowed luminance, etc.) and instantly transitions to new brightness, which can look distracting and nauseating, thus limiting its usability when watching content or playing video games.

This tool makes DCR technology more viable to use on any external SDR monitor with or without native DCR support, as long as the monitor's brightness (backlight) can be controlled from the OS side using the DDC/CI protocol, which is almost always available on HDMI-supported monitors.

It can also be used to fake "HDR" effects, but instead of the display making **Local Dimming** (Full Array or Edge-Lit) adjustments (as in the case of a real HDR display), it will only be making **Global Dimming** (Direct-Lit) adjustments along with content gamma adjustments to compensate for poor black visibility under low backlight and blinding highlights under high backlight. This is vastly inferior to real HDR but may still be convincing to your eyes.

<br>
<img src="docs/Dimming_Techniques.gif" alt="Backlight Dimming Technologies - Courtesy of Wikipedia" width="500">

## WARNING (PLEASE READ THIS VERY CAREFULLY)

Many old or cheap monitors use EEPROM to save monitor settings in a non-volatile way. The problem is there are limits to the number of writes (often around 100,000) that can be done before the EEPROM (and even the monitor) is permanently damaged. Even though this program is not saving settings explicitly to the display's non-volatile storage, there is no way to know whether your monitor writes to EEPROM after every brightness change.

Search for your monitor model's technical specifications on the internet. If there is any mention of "EEPROM" storage anywhere in the documentation, then please DON'T use this program.

**I AM NOT RESPONSIBLE IF YOU END UP TOASTING YOUR MONITOR AFTER USING THIS PROGRAM FOR A WHILE. YOU HAVE BEEN WARNED.**

For references:

[https://news.ycombinator.com/item?id=24344696](https://news.ycombinator.com/item?id=24344696)

## Limitations

Functions that retrieve and set the monitor's brightness value take a minimum of 40 milliseconds and 50 milliseconds respectively, which may feel slower in response compared to the native dynamic contrast technology of your monitor. This is a hard limit (i.e., an I/O bottleneck) with no way around it.

For references:

[GetVCPFeatureAndVCPFeatureReply](https://learn.microsoft.com/en-us/windows/win32/api/lowlevelmonitorconfigurationapi/nf-lowlevelmonitorconfigurationapi-getvcpfeatureandvcpfeaturereply)

[SetVCPFeature](https://learn.microsoft.com/en-us/windows/win32/api/lowlevelmonitorconfigurationapi/nf-lowlevelmonitorconfigurationapi-setvcpfeature)

## Requirements

Your monitor must support DDC/CI for backlight adjustments to work. If it is supported, make sure it is enabled in your monitor's OSD. Without software backlight control, you can't use the monitor luminance adjustment feature, but you may still benefit from the content-adaptive gamma adjustments.

For content gamma adjustments to work, Windows must have a default system-level gamma ramp generated either by selecting a color profile in Display Settings or by running the Windows calibration tool.

Only Windows is supported at the moment.

## Recommended usage: backlight here, tone mapping in ReShade

The content gamma (tone curve) adjustment is **disabled by default** (`GAMMA_RAMP_ADJUSTMENTS = False`). It drives the *system-level* gamma ramp, which is a coarse, global control: it applies to the whole desktop, is easy to make look wrong (washed-out shadows, banding), and cannot do anything perceptually smarter than a global curve.

For games, a proper in-engine post-process shader does the tone mapping far better than the gamma ramp ever could. I recommend running this program purely for the **backlight (DDC/CI) side** and letting [ReShade](https://reshade.me/) handle the content side with the **PHDR2.fx** shader I wrote for exactly this pairing:

- **PHDR2.fx**: <https://github.com/danyalziakhan/dz-shaders/blob/main/Shaders/PHDR2.fx>

The two complement each other: this program adapts the physical backlight to the scene (deepening blacks in dark scenes, opening up bright ones), while PHDR2.fx reshapes the in-game image with a real per-frame tone curve that has none of the gamma-ramp's global limitations. Install ReShade for your game, add PHDR2.fx, and keep `GAMMA_RAMP_ADJUSTMENTS = False` here.

If you are not gaming (e.g. video or general desktop use) and still want the content-side lift, you can set `GAMMA_RAMP_ADJUSTMENTS = True`, but keep expectations modest and the `TONE_CURVE_STRENGTH` low.

## Optional features

- **Dynamic contrast (DDC/CI VCP `0x12`)**: set `CONTRAST_ADJUSTMENTS = True` to also adapt the monitor's contrast control — raising it in dark scenes and lowering it in bright ones. It is probed at startup and silently disabled if your monitor doesn't report contrast support. Contrast and brightness share one writer thread (so they interleave on the single I2C bus and share the same per-minute write budget).
- **Auto black-bar detection** (`AUTO_BLACK_BAR_DETECTION`, on by default): letterbox/pillarbox bars are detected each frame and excluded from the scene measurement, so a 21:9 film on a 16:9 screen is judged by the image rather than the black bars.
- **Write-count logging**: each throttled status line and the shutdown message report the number of DDC/CI writes this session, for EEPROM-budget visibility.
- **`--dry-run`**: `uv run main.py --dry-run` prints every brightness/contrast/gamma decision **without touching the hardware** — useful for tuning `config.py` safely.
- **Tuning aids**: `test_vcp_latency.py` measures your monitor's write latency and safe cadence; `test_flicker_pattern.py` shows a fullscreen sequence of known-APL scenes so you can watch the response and tune the smoothing/hysteresis objectively.

## Installation

1. Install **uv**: <https://github.com/astral-sh/uv>
2. Clone the repository: `git clone https://github.com/danyalziakhan/better-dynamic-contrast-ratio.git`
3. Navigate to the project directory: `cd better-dynamic-contrast-ratio`
4. Run the program: `uv run main.py` (or use the `run.bat` script)

> **Note**: `uv run` will automatically create the virtual environment and install the required Python version (3.13) and dependencies on the first run.

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.
