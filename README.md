# Better Dynamic Contrast Ratio (DCR) - [PROOF OF CONCEPT]

Software implementation of the **Dynamic Contrast Ratio (DCR)** technology (sometimes also known as **Advanced Contrast Ratio (ACR)**, **Smart Contrast Ratio (SCR)** and other similar names) that is found in many SDR monitors but often lack support for any kind of manual adjustments (such as the minimum and maximum allowed luminance, etc.) and would instantly transition to new brightness which can look distracting and nauseating thus limiting its usability when watching content or playing video games.

This tool makes DCR technology more viable to use on any kind of external SDR monitor with or without native DCR support as long as monitor's brightness (backlight) can be controlled from the OS side using DDC/CI protocol that is almost always available in HDMI supported monitors.

It can also be used to fake the "HDR" effects, but instead of the display making **Local Dimming** (Full Array or Edge-Lit) adjustments (as in the case of real HDR display), it will only be making **Global Dimming** (Direct-Lit) adjustments (along with content gamma adjustments to compensate for poor blacks visibility under low backlight and blinding highlights under high backlight) which is vastly inferior to real HDR but may still be convincing to your eyes.

<br>
<img src="docs/Dimming_Techniques.gif" alt="Backlight Dimming Technologies - Courtesy of Wikipedia" width="500">

## WARNING (PLEASE READ THIS VERY CAREFULLY)

Many old/cheap monitors use EEPROM to save monitor settings in a non-volatile way. The problem is there are limits to the number of writes (often around 100,000) that can be done before the EEPROM (and even the monitor) is toasted. Even though this program is not saving the settings explicitly to the display's non-volatile storage, there is no way to know your monitor is not writing to EEPROM after every brightness change.

Search for your monitor model's technical specifications on the internet. If there is any mention of "EEPROM" storage anywhere in the documentation, then please DON'T use this program.

**I AM NOT RESPONSIBLE IF YOU END UP TOASTING YOUR MONITOR AFTER USING THIS PROGRAM FOR A WHILE. YOU HAVE BEEN WARNED.**

For references:

[https://news.ycombinator.com/item?id=24344696](https://news.ycombinator.com/item?id=24344696)

## Limitations

Functions that retrieve and set the monitor's brightness value will take a minimum 40 milliseconds and 50 milliseconds, respectively, which may feel slower in response compared to the native dynamic contrast technology of your monitor. It's a hard limit (i.e., an I/O bottleneck), meaning there is no way around it.

The only potential way around it is to output the duplicated screen capture in a separate window with a delay of 40/50 ms, but that is out of scope of this project. If you are using the Lossless Scaling application, then you can mimic this behavior by using frame generation and adding some frame delay (i.e., increase max frame latency).

For references:

[GetVCPFeatureAndVCPFeatureReply](https://learn.microsoft.com/en-us/windows/win32/api/lowlevelmonitorconfigurationapi/nf-lowlevelmonitorconfigurationapi-getvcpfeatureandvcpfeaturereply)

[SetVCPFeature](https://learn.microsoft.com/en-us/windows/win32/api/lowlevelmonitorconfigurationapi/nf-lowlevelmonitorconfigurationapi-setvcpfeature)

## Requirements

Your monitor must support DDC/CI for backlight adjustments to happen. If it's supported, then make sure it is enabled in your monitor's OSD. Without software backlight adjustments support, you can't use monitor's luminance adjustment feature, but you may still benefit from content adaptive gamma adjustments.

For content gamma adjustments to happen, Windows must have a default system-level gamma ramp generated either by selecting a color profile (in "Display settings") or making callibration using Windows callibration tool.

Only Windows is supported at the moment.

## Installation

Install Python 3.13.4: <https://www.python.org/ftp/python/3.13.4/python-3.13.4-amd64.exe>

1. Clone the repository: `git clone https://github.com/danyalzia/better-dynamic-contrast-ratio.git`
2. Navigate to the project directory: `cd better-dynamic-contrast-ratio`
3. Create Python virtual environment: `python -m venv .venv`
4. Install dependencies: `pip install -r requirements.txt`
5. Run the program `python main.py` (or use `run.bat` script)

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.
