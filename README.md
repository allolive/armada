# Armada OS for the AYN Odin 3

Unofficial [Armada OS](https://github.com/armada-os/armada) builds for the AYN
Odin 3 (SM8750 / Adreno 830). Everything is Armada exactly as they ship it,
plus the changes on this branch. Not an official Armada release; no warranty.

## Branches

- **`upstream`** — an exact mirror of `armada-os/armada` `main`. Nothing of ours
  is committed there. It is force-pushed to follow upstream wherever they go,
  including across a history rewrite.
- **`odin3`** — this branch, the default. It shares no history with the mirror
  and holds only what we add or change.
- **`main`** — generated: the mirror's tip plus one commit laying `odin3` over
  it. Never edit it; it is overwritten on every sync. `git log upstream..main`
  is exactly our delta.

The sync job (`.github/workflows/odin3-sync.yml`) runs daily, on every push to
`odin3`, and by hand. When the assembled tree differs from `main` it pushes, and
upstream's own `build.yml` builds `main` and publishes
`ghcr.io/allolive/armada:testing`, signed with our key. The image pipeline is
upstream's, unmodified.

## Layout

| path | what | rule |
|---|---|---|
| `overlay/` | files we **add**, at their path in the tree | refused if upstream already has the path |
| `tree-patches/` | changes to files upstream **owns**, applied in name order | plain `git apply`; one that no longer applies stops the build |
| `kernel-series.append` | lines appended to `packages/kernel/patches/series` | refused if upstream's series already names the patch |
| `scripts/odin3-assemble.sh` | lays all three over an upstream checkout | |

Assembly also refuses a tree whose `policy.json` does not trust
`ghcr.io/allolive/armada`: such an image installs fine and then rejects every
update after it.

Current changes:

- **Signing** (`01`) — the image trusts `ghcr.io/allolive/armada`, signed with our key.
- **Build plumbing** (`10`, `20`) — copy any package upstream has already built
  with the same content hash, so only packages we change get built here; Steam's
  branch picker and the Armada OS page treat our repository like upstream's.
- **Refresh rate** — kernel `0138` adds 30..120Hz modes to the ICNA3520 panel
  (rebased onto upstream's 7.2.6 driver, change lines identical to the version
  that ran on the device); gamescope-session `9001` lets the Odin 3 use them
  (upstream's quirk pinned it to 60/120); gamescope `9001` prefers an exact panel
  rate over refresh doubling. This is the setup that ran daily from 2026-08-12,
  then as a user environment.d override.
- **GPU undervolt** — `a6xx_uv` is built inside the kernel package (`40`), so it
  always matches its kernel; the `adreno-uv` Decky plugin and its `gpustress`
  load generator are built into the image (`41`) and the plugin's
  `bin/a6xx_uv.ko` links to the module in `/usr/lib/modules`. Nothing is applied
  until a profile is selected in the plugin.

## Working on it locally

```sh
git fetch fork upstream odin3
git worktree add ../armada-build fork/upstream --detach
cd ../armada-build && bash ../armada-fork/scripts/odin3-assemble.sh ../armada-fork
```

To change a file upstream owns: edit it in the assembled tree, then
`git diff -- <paths> > ../armada-fork/tree-patches/NN-what-it-does.patch`.

## Secrets

- `SIGNING_SECRET` — cosign private key (empty password); `build.yml` refuses to
  publish without it. The public half ships in the image as
  `/etc/pki/containers/allolive.pub`.
- `ODIN3_PUSH_TOKEN` — fine-grained token on this repository, Contents and
  Workflows read/write. The job token cannot push upstream's workflow changes.

## Switching a device to these builds

The device must trust the key before the first switch (its `/etc` policy, the
same entry the image carries), then:

```sh
sudo bootc switch --enforce-container-sigpolicy ghcr.io/allolive/armada:testing
```
