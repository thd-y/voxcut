# Third-party notices

VoxCut itself is under the MIT License ([LICENSE](LICENSE)). It includes or adapts the following.

## Silero VAD

`segment_raw` in [voxcut/intervals.py](voxcut/intervals.py) is ported from silero-vad 6.2.3
(`silero_vad/utils_vad.py`, `get_speech_timestamps_from_probs`). The model itself is installed
as a Python dependency, not vendored. https://github.com/snakers4/silero-vad

```
MIT License

Copyright (c) 2020-present Silero Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Vendored UI assets

In [voxcut/ui/static/vendor/](voxcut/ui/static/vendor/), each with its license file next to it:

- Basecoat CSS: MIT, [LICENSE.md](voxcut/ui/static/vendor/LICENSE.md). Built with Tailwind CSS: MIT, [LICENSE-tailwindcss.txt](voxcut/ui/static/vendor/LICENSE-tailwindcss.txt).
- Lucide icons (the sprite, and the icon in [img/favicon.svg](voxcut/ui/static/img/favicon.svg)): ISC, [LICENSE-lucide.txt](voxcut/ui/static/vendor/LICENSE-lucide.txt).
- Mulish font: SIL Open Font License 1.1, [OFL-mulish.txt](voxcut/ui/static/vendor/OFL-mulish.txt).
