# LWA Lettermark SVG Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add three 500 × 500 SVG lettermark alternatives based on the LWA initials.

**Architecture:** The assets use manually drawn letter paths rather than dependent font files. Each option pairs a distinct letter construction with a separate palette: connected teal, modular coral, and negative-space purple with a yellow status signal.

**Tech Stack:** SVG 1.1, XML validation, macOS SVG-to-PNG rendering.

---

### Task 1: Create the three lettermark assets

**Files:**
- Create: `design/logo/lwa-lettermark-a-connected.svg`
- Create: `design/logo/lwa-lettermark-b-modular.svg`
- Create: `design/logo/lwa-lettermark-c-negative.svg`

**Step 1: Draw portable letterforms**

Use only paths, rects, circles, and lines. Set every SVG to `width="500"`, `height="500"`, and `viewBox="0 0 500 500"`; include title and description metadata.

**Step 2: Apply distinct visual systems**

Use teal for the connected letterform, coral and warm-peach for modular tiles, and deep violet with a yellow health dot for the negative-space construction. Do not use gradients or external fonts.

### Task 2: Verify delivery

**Files:**
- Test: `design/logo/lwa-lettermark-a-connected.svg`
- Test: `design/logo/lwa-lettermark-b-modular.svg`
- Test: `design/logo/lwa-lettermark-c-negative.svg`

**Step 1: Validate XML and required dimensions**

Run: `xmllint --noout design/logo/lwa-lettermark-*.svg`

Expected: exit code 0.

**Step 2: Render and inspect**

Run: `sips -s format png design/logo/lwa-lettermark-*.svg --out /tmp/lwa-lettermark-preview/`

Expected: each file produces a 500 × 500 PNG with legible letters and differentiated palette.

**Step 3: Synchronize completed work**

Append a completed Chinese record to `task-list.md` using the project CLI and run its `check` command.
