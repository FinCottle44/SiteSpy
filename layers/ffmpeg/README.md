# ffmpeg Lambda Layer

This directory is packaged as a Lambda layer (`FfmpegLayer` in `template.yaml`,
`ContentUri: layers/ffmpeg/`). At runtime the layer contents are extracted under
`/opt`, so the binary placed at `layers/ffmpeg/bin/ffmpeg` becomes available at
**`/opt/bin/ffmpeg`** — which is the default `FFMPEG_PATH` used by the timelapse
worker (`sitespy/config.py`).

```
layers/ffmpeg/
├── README.md      <- this file
└── bin/
    └── ffmpeg     <- static binary (NOT yet committed — see below)
```

## ⚠️ Manual step required before deployment

The static `ffmpeg` binary (~30–80 MB) is **not** included in this repository and
must be downloaded and placed at `layers/ffmpeg/bin/ffmpeg` before running
`sam build` / `sam deploy`. Deployment will produce a non-functional worker until
this binary is present.

## Required binary

- **Architecture:** `arm64` / `aarch64` — MUST match the Lambda `Architectures: [arm64]`
  setting in `template.yaml`. An x86_64 build will fail to execute on the function.
- **Type:** fully static build (no dynamic library dependencies), so it runs on the
  Amazon Linux 2 / AL2023 Lambda runtime without extra shared objects.
- **Source:** John Van Sickle static builds — https://johnvansickle.com/ffmpeg/
  (widely used for Lambda; provides static `arm64` release and git snapshot builds).

## Install instructions

Run these commands from the `sitespy/` directory (where `template.yaml` lives):

```bash
# 1. Download the static arm64 release build
curl -L -o /tmp/ffmpeg-arm64.tar.xz \
  https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz

# 2. Extract
mkdir -p /tmp/ffmpeg-extract
tar -xf /tmp/ffmpeg-arm64.tar.xz -C /tmp/ffmpeg-extract --strip-components=1

# 3. Place ONLY the ffmpeg binary into the layer (ffprobe is not needed)
cp /tmp/ffmpeg-extract/ffmpeg layers/ffmpeg/bin/ffmpeg

# 4. Make it executable (required — Lambda will exec it directly)
chmod +x layers/ffmpeg/bin/ffmpeg

# 5. Verify the architecture is aarch64/ARM
file layers/ffmpeg/bin/ffmpeg
#   expected: ELF 64-bit LSB ... ARM aarch64 ...

# 6. (Optional, on an arm64 host) confirm it runs and record the version
layers/ffmpeg/bin/ffmpeg -version | head -n 1
```

Record the exact version you installed here for traceability:

- **Downloaded from:** https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz
- **Version installed:** _fill in (e.g. `ffmpeg version 6.1.1-static ...`) after step 6_
- **Date installed:** _fill in_

## Committing the binary

The binary is large, so choose one of:

- **git-lfs (recommended):**
  ```bash
  git lfs install
  git lfs track "sitespy/layers/ffmpeg/bin/ffmpeg"
  git add .gitattributes sitespy/layers/ffmpeg/bin/ffmpeg
  git commit -m "Add static arm64 ffmpeg binary for timelapse layer"
  ```
- **Commit directly** (only if repo size is not a concern), ensuring the executable
  bit is preserved (`git update-index --chmod=+x sitespy/layers/ffmpeg/bin/ffmpeg`).

Either way, make sure the executable permission bit survives the commit — the worker
invokes the binary directly via `subprocess.run`.

## Why arm64

The stack's `Globals` set `Architectures: [arm64]`, and the timelapse worker
(`TimelapseWorkerFunction`) attaches this layer and runs on arm64. The ffmpeg
binary's architecture must match the function architecture exactly, otherwise the
`subprocess.run([FFMPEG_PATH, ...])` call fails with an exec-format error at runtime.
