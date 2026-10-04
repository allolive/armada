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
| `scripts/odin3-assemble.sh` | lays both over an upstream checkout | |

Assembly also refuses a tree whose `policy.json` does not trust
`ghcr.io/allolive/armada`: such an image installs fine and then rejects every
update after it.

Current changes:

- `10-packages-reuse-upstream-builds` — copy any package upstream has already
  built with the same content hash, so only packages we change get built here.
- `20-updater-recognise-fork-repository` — Steam's branch picker and the Armada
  OS page treat our repository like upstream's.

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
