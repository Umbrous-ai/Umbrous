# Umbrous agent harness (front-end prototype)

Open `umbrous-harness.html` in a browser. It is self-contained (no build, no network).

Layout: floating sidebar and main panels. The sidebar has the workspace switcher, **New chat**, a collapsible **Projects** dropdown, and a collapsible **Sessions** list. The main panel is a chat with result cards. Sending a message plays a fake agent run.

## How your tokens were mapped to dark

| Your token (light) | Dark value used | Note |
|---|---|---|
| text subtle `#7F7F7F` | `#7F7F7F` | Mid gray reads fine on both themes |
| text default `#5D5D5D` | `#B5B5B5` | Inverted |
| text strong `#292929` | `#F2F2F2` | Inverted |
| bg-selected `#F5F5F5` | `#292929` | Inverted |
| border `#F2F2F2` | `#2A2A2A` | Inverted |

Your light values are kept as `--light-*` variables in the CSS for a future light theme.

## Things the spec was missing (placeholders I picked)

- **Backgrounds**: canvas `#141414`, app `#1B1B1B`, raised/cards `#222222`, hover `#232323` (sampled from reference 1).
- **Accent**: violet `#8B7CF6` (badges, unread dots, charts). Swap for your brand color.
- **Status colors**: danger `#F26D6D`, success `#5CCB8A`, warning `#F2C14E`.
- **Radius**: 4 / 6 / 8 / 12 / full.
- **Spacing**: 4 / 8 / 12 / 16 / 24 / 32 / 48.
- **Mono font**: SF Mono, for commands and code.
- **Line height**: 1.5 for UI text, 1.6 for chat answers.

## Fonts and icons

- SF Pro via the system font stack, so it renders as SF Pro on a Mac. Only weights 400 and 500 are used.
- Icons are hand-drawn Nucleo-style outlines on a 16px grid at a 1px stroke. Nucleo is a paid set, so swap them for your licensed files; the names are in the SVG sprite at the top of each file.

## Since M0

`bin/umbrous` now serves this page from the local Python runtime at `http://127.0.0.1:8417/`. The only changes here are a **Runtime** section at the top of the Details panel (filled by `umbrous/web/runtime-bridge.js` from a real health call) and the script tag that loads it. Opened directly as a file (or with `bin/umbrous prototype`) it behaves as before and the Runtime section reads "Not connected".

## Since M1

When connected to the runtime, `umbrous/web/chat.js` takes over the composer: the model pill picks an installed Ollama model, Send streams a real reply (and turns into Stop while it runs), and the Details panel shows the exact model and the context used. Ids were added to the model pill, Session ▸ Model and the Context bar for this. Sidebar sessions/projects and the Tools, Files and Steps sections are still placeholders.

## Since M2

The sidebar's Projects and Sessions now come from the runtime's history store: the placeholder entries are replaced on connect, the **+** next to Projects adds a project, and clicking a chat loads its saved transcript. The header's Options button (now `#chat-options`) opens Archive / Restore and a disabled Delete. Session ▸ Project got an inner `#sess-project` span. New CSS is under `/* M2: history */`. The inline prototype script is unchanged; the new sidebar buttons stop their click events so its placeholder handler never sees them.
