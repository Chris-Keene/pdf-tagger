# Installing Trellis

**Trellis, formerly PDF Tagger**, is an accessibility tagger for PDFs. The latest
version is always at <https://github.com/Chris-Keene/trellis/releases>, and there is
more about it at <https://chriskeene.me/trellis>.

## Upgrading from PDF Tagger

Trellis is the new name for PDF Tagger, from version 1.22.0.

- **It installs as a separate app.** Once Trellis is in Applications, move
  **PDF Tagger** to the Bin.
- **Files you tagged with PDF Tagger** open in Trellis with your decisions intact,
  the same as before.
- **macOS will stop it once on first launch**, exactly as it did for PDF Tagger,
  because to macOS it is a new app. The steps below clear it.
- **The log has moved** to `~/Library/Logs/Trellis.log`.

---

## What you need

A Mac with **Apple silicon** (M1 or later — any Mac bought since about 2021).
Nothing else needs installing, and it works offline.

> **On an older Intel Mac it will not open at all.** If double-clicking does
> nothing, or you get "the application cannot be opened", check
> &#63743; menu → About This Mac. If it says "Intel", this build won't run.
> Say so and it can be built for you.

---

## Installing it (about a minute)

**1. Unzip the file.** Double-click `Trellis-1.22.0.zip`. You'll get an app
called **Trellis**.

**2. Drag it into your Applications folder.** Not essential, but it's where you'll
expect to find it later.

**3. Double-click it.** The first time, macOS will refuse to open it and show a
message about not being able to check it for malicious software. **This is
expected.** It happens because the app isn't registered with Apple, not because
anything is wrong with it. See below for how to get past it once.

---

## Getting past the first-launch warning

You only ever do this once.

1. Double-click **Trellis**. A message appears saying macOS can't verify it.
   Click **Done** (or **OK**, or **Cancel** — whichever it offers).
2. Open **System Settings** (the grey cog in your Dock, or ⌘-Space and type
   "System Settings").
3. Click **Privacy & Security** in the left-hand list.
4. Scroll down to the **Security** section. You'll see a line saying
   *"Trellis was blocked to protect your Mac."*
5. Click **Open Anyway** next to it.
6. Confirm with your Mac password, Touch ID, or Apple Watch.
7. The app opens. From now on it opens normally, like anything else.

> **Why does this happen?** Apple charges developers an annual fee to register
> apps so they open without a warning. This one is an internal tool that hasn't
> been through that, so macOS treats it as unknown. It isn't a sign of a problem.

### Faster route, if you're comfortable with Terminal

Skips all of the above. Open Terminal, paste this, press Return:

```bash
xattr -d -r com.apple.quarantine "/Applications/Trellis.app"
```

Then open the app normally. (If you put it somewhere other than Applications,
change the path to match.)

---

## Using it

1. **Open the app** and drop a PDF onto the window, or click **Choose a PDF**.
   You can also right-click any PDF and pick **Open With → Trellis**.
2. **Follow the five steps** along the top. It explains each one as you go.
3. **Leave "Help tips on"** while you're learning. When the explanations start
   getting in the way, turn them off with the button in the top right.
4. **Your original file is never changed.** The last step saves a new, tagged copy
   alongside it.

If a document has more than one page, the app walks you through them one at a
time. The strip of page thumbnails shows what's still outstanding on each, and the
big button at the bottom right takes you to the next page rather than skipping to
the end.

---

## If something goes wrong

- **It won't open at all, even after "Open Anyway".** Move it to your Applications
  folder and try again. Apps run from inside the Downloads folder sometimes behave
  oddly.
- **It says the PDF is password protected.** It is. Remove the password on the
  original and try again.
- **It says some text can't be tagged.** Some PDFs store their text in a way this
  app can't reach. It tells you rather than pretending, and the fix is to
  re-export the document from wherever it was made.
- **It says a font isn't stored in the PDF.** Same idea: that can't be fixed by
  tagging, only by re-exporting with the fonts embedded.
- **It says the tagged copy will not be password protected.** Correct. Tagging has
  to rewrite the file and it comes out unprotected. Re-apply the password to the
  copy if it matters.
- **Anything else.** There's a log at
  `~/Library/Logs/Trellis.log`; send that on with a description of what you
  were doing.

---

## Notes for whoever is handing this out

**What to send:** `Trellis-1.22.0.zip` (89MB; Finder says 93.6 MB), attached to
each [release](https://github.com/Chris-Keene/trellis/releases) or built into
`dist/`. Zipped with `ditto`, so the code signature survives the transfer. Slack and Drive handle that comfortably; email
probably will not, so use a link.

**Every recipient will hit the Gatekeeper warning**, because the app is ad-hoc
signed rather than signed with a Developer ID. Verified: a received copy picks up
the quarantine flag and `spctl` rejects it. On macOS 15 the Control-click → Open
shortcut no longer works for this, so the System Settings route above is the one
to give people.

**If that first-run friction isn't acceptable** for a wider rollout, the fix is a
Developer ID plus notarisation: an Apple Developer account (~£79/$99 a year), then
signing and stapling the build. After that it opens with no warning at all and
these instructions shrink to "unzip, drag to Applications, open". Worth doing if
this goes past a handful of people; overkill for a pilot.

**Apple silicon only.** The build is arm64. An Intel Mac cannot run it. PyInstaller
builds for the host architecture, so a universal build would need universal2 wheels
for pikepdf and PyMuPDF — doable, not free. Worth checking nobody in the group is
still on an Intel machine before you send it.

**veraPDF is bundled**, so recipients do get the real conformance verdict without
installing Java or Homebrew. It is the actual veraPDF CLI plus a `jlink`-trimmed
OpenJDK runtime (63MB once unzipped), verified to produce a verdict identical to a
system install with no Java anywhere on PATH.

That is what takes the zip from 44MB to 89MB. `./build/make_app.sh --no-verapdf`
builds the 44MB version, which tags identically but shows "Exported, not verified
here" instead of a pass or fail.

**Licensing**, since this redistributes other people's code: veraPDF is dual
GPL-3.0-or-later / MPL-2.0 and is included under **MPL-2.0**, which is file-level
copyleft and imposes nothing on our source. The runtime is OpenJDK under GPLv2 **with
the Classpath Exception**, which is precisely what permits shipping a trimmed runtime
inside an application. Both licence texts and the JDK's per-module notices are in
`Contents/Resources/verapdf/`.

**It needs nothing installed** and makes no network connections. It runs a server
on `127.0.0.1` for its own window to talk to, on a random port, which never leaves
the machine.
