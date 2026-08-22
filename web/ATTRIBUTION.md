# Attribution and licensing

## This dashboard

Part of auto-ffe-reviewer, GPL-3.0-or-later. **Unofficial community tool.** Not
affiliated with, endorsed by, or published by Canonical Ltd.

## Typography

The page asks for the Ubuntu font family and falls back to the system UI font:

```css
font-family: "Ubuntu Sans", "Ubuntu", system-ui, -apple-system, "Segoe UI", sans-serif;
```

**No font files are shipped with this repository**, for two reasons worth
recording rather than rediscovering:

1. The Ubuntu Font Family in Ubuntu's own `fonts-ubuntu` package is under the
   **Ubuntu Font Licence 1.0**, which Debian does not consider DFSG-free — the
   package sits in multiverse. Redistributing it from a GPL-3.0 repository
   invites a licence question that vendoring cannot answer.
2. UFL 1.0 requires that *modified* versions be renamed. A subsetted web font
   is a modified version, so shipping one correctly would mean renaming the
   family, which defeats the point of asking for it by name.

The practical effect is nil for the intended audience: Release Team members run
Ubuntu, where `fonts-ubuntu` is installed and the family resolves locally at no
download cost. Elsewhere the page renders in the system UI font, which is
perfectly legible.

To install the fonts locally:

```sh
sudo apt install fonts-ubuntu
```

If you would rather self-host, subset the family yourself and satisfy UFL 1.0's
renaming requirement, or use the OFL-licensed releases from
<https://design.ubuntu.com/font>.

## Colour

Ubuntu's brand palette is used for accents and status. Colours are values, not
assets, and carry no licence.

Ubuntu orange (`#E95420`) has a contrast ratio of roughly 3:1 on white, which
**fails WCAG AA for body text**. It is used only for borders, rules and
decorative accents. Status is always carried by a text label as well as colour,
so nothing on the page depends on colour perception alone.

## Trademarks

The Circle of Friends logo and the Ubuntu word mark are Canonical trademarks
with a restrictive usage policy, and **neither is used here**. The dashboard
uses a plain typographic wordmark.

Ubuntu and Canonical are registered trademarks of Canonical Ltd.
