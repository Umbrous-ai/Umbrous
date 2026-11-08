# Moicons activity assets

Adapted from https://github.com/akshat-jp/moicons at
`1769dac960703c40fe910b76e3f82b74984915f0` (icons/*.tsx).
Copyright © 2026 Akshat Verma. See LICENSE.txt for the permission notice.
The upstream components identify their geometry as Tabler icons; the Tabler
notice is retained in TABLER-LICENSE.txt. Both notices ship in app Resources.

These are Moicons path geometries converted from React/Motion components to
static SVGs for GPUI. The padded website viewBox is replaced with the native
24px drawing grid. A 1.5-unit stroke renders at 1 logical pixel at Umbrous's
16px activity-icon size. GPUI supplies the foreground color.

Umbrous animates the loader with a continuous native rotation while thinking;
other symbols remain still. React hover animations are not bundled or executed.
The brand and other interface icons remain separate.

Local name → upstream component:

- cpu → loader.tsx
- doc → file-description.tsx
- search → search.tsx
- pen → pencil.tsx
- terminal → code.tsx
- folder → folder.tsx
- clock → clock.tsx
- shield → shield.tsx
- shield-check → circle-check.tsx
- list-check → check.tsx
- bolt → bolt.tsx
