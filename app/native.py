"""A native Mac window around the app, instead of a browser tab.

The UI itself is still the HTML in `app/ui`, shown in a `WKWebView`. That is the
standard shape for this kind of tool and it keeps the whole tested engine intact:
rewriting the five-step interface in AppKit would be weeks, and rewriting the PDF
work in Swift is not even possible, because PDFKit cannot write a structure tree.

What the native shell actually buys:
  * a real window, with no URL bar and no browser tab
  * a real menu bar, which is also what makes Cmd-C/V/X/A work inside text fields
  * native open and save panels that are properly modal to the app
  * Dock behaviour, Cmd-Tab, and dropping a PDF onto the icon to open it
"""
import os
import threading

import objc
from AppKit import (NSApp, NSApplication, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSMakeRect, NSMenu, NSMenuItem,
                    NSModalResponseOK, NSObject, NSOpenPanel, NSSavePanel,
                    NSScreen, NSWindow, NSWindowStyleMaskClosable,
                    NSWindowCollectionBehaviorFullScreenPrimary,
                    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled)
from Foundation import NSURL, NSURLRequest, NSOperationQueue
from WebKit import WKWebView, WKWebViewConfiguration

APP_NAME = "Trellis"
_state = {"window": None, "webview": None, "delegate": None}


def on_main(fn, timeout=900):
    """Run `fn` on the main thread and wait for it.

    Cocoa panels must run on the main thread, but the HTTP handler that asks for
    one is on a worker thread, so the call has to hop over and block.
    """
    if NSOperationQueue.mainQueue() == NSOperationQueue.currentQueue():
        return fn()
    box, done = {}, threading.Event()

    def block():
        try:
            box["value"] = fn()
        except Exception as exc:                      # noqa: BLE001
            box["error"] = exc
        finally:
            done.set()

    NSOperationQueue.mainQueue().addOperationWithBlock_(block)
    if not done.wait(timeout):
        raise TimeoutError("the dialog was left open too long")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _allow_pdf(panel):
    # setAllowedFileTypes_ is deprecated but still works; UTType is the modern
    # route and is not on every build, so try it and fall back.
    try:
        from UniformTypeIdentifiers import UTTypePDF
        panel.setAllowedContentTypes_([UTTypePDF])
        return
    except Exception:
        pass
    try:
        panel.setAllowedFileTypes_(["pdf"])
    except Exception:
        pass


def choose_pdf():
    def go():
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setMessage_("Choose a PDF to make accessible")
        panel.setPrompt_("Open")
        _allow_pdf(panel)
        if panel.runModal() != NSModalResponseOK:
            return None
        urls = panel.URLs()
        return str(urls[0].path()) if urls and len(urls) else None
    return on_main(go)


def choose_save(default_name, default_dir):
    def go():
        panel = NSSavePanel.savePanel()
        panel.setMessage_("Save the accessible PDF")
        panel.setPrompt_("Save")
        panel.setNameFieldStringValue_(default_name)
        if default_dir and os.path.isdir(default_dir):
            panel.setDirectoryURL_(NSURL.fileURLWithPath_(default_dir))
        _allow_pdf(panel)
        if panel.runModal() != NSModalResponseOK:
            return None
        url = panel.URL()
        return str(url.path()) if url else None
    return on_main(go)


def _item(menu, title, action, key, target=None):
    it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
    if target is not None:
        it.setTarget_(target)
    menu.addItem_(it)
    return it


def build_menus(delegate):
    bar = NSMenu.alloc().init()

    app_item = NSMenuItem.alloc().init()
    bar.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    _item(app_menu, f"About {APP_NAME}", "orderFrontStandardAboutPanel:", "")
    app_menu.addItem_(NSMenuItem.separatorItem())
    _item(app_menu, f"Hide {APP_NAME}", "hide:", "h")
    _item(app_menu, "Hide Others", "hideOtherApplications:", "")
    _item(app_menu, "Show All", "unhideAllApplications:", "")
    app_menu.addItem_(NSMenuItem.separatorItem())
    _item(app_menu, f"Quit {APP_NAME}", "terminate:", "q")
    app_item.setSubmenu_(app_menu)

    file_item = NSMenuItem.alloc().init()
    bar.addItem_(file_item)
    file_menu = NSMenu.alloc().initWithTitle_("File")
    _item(file_menu, "Open PDF…", "openDocument:", "o", target=delegate)
    file_menu.addItem_(NSMenuItem.separatorItem())
    _item(file_menu, "Close Window", "performClose:", "w")
    file_item.setSubmenu_(file_menu)

    # Without a real Edit menu, Cmd-C / Cmd-V / Cmd-A do nothing inside the web
    # view's text fields, which makes writing a description miserable.
    edit_item = NSMenuItem.alloc().init()
    bar.addItem_(edit_item)
    edit_menu = NSMenu.alloc().initWithTitle_("Edit")
    _item(edit_menu, "Undo", "undo:", "z")
    _item(edit_menu, "Redo", "redo:", "Z")
    edit_menu.addItem_(NSMenuItem.separatorItem())
    _item(edit_menu, "Cut", "cut:", "x")
    _item(edit_menu, "Copy", "copy:", "c")
    _item(edit_menu, "Paste", "paste:", "v")
    _item(edit_menu, "Select All", "selectAll:", "a")
    edit_item.setSubmenu_(edit_menu)

    view_item = NSMenuItem.alloc().init()
    bar.addItem_(view_item)
    view_menu = NSMenu.alloc().initWithTitle_("View")
    _item(view_menu, "Reload", "reloadPage:", "r", target=delegate)
    _item(view_menu, "Actual Size", "actualSize:", "0", target=delegate)
    _item(view_menu, "Zoom In", "zoomIn:", "+", target=delegate)
    _item(view_menu, "Zoom Out", "zoomOut:", "-", target=delegate)
    view_menu.addItem_(NSMenuItem.separatorItem())
    _item(view_menu, "Enter Full Screen", "toggleFullScreen:", "f")
    view_item.setSubmenu_(view_menu)

    window_item = NSMenuItem.alloc().init()
    bar.addItem_(window_item)
    window_menu = NSMenu.alloc().initWithTitle_("Window")
    _item(window_menu, "Minimise", "performMiniaturize:", "m")
    _item(window_menu, "Zoom", "performZoom:", "")
    window_item.setSubmenu_(window_menu)

    NSApp().setMainMenu_(bar)
    NSApp().setWindowsMenu_(window_menu)


class Delegate(NSObject):
    def initWithOnOpen_(self, on_open):
        self = objc.super(Delegate, self).init()
        if self is None:
            return None
        self._on_open = on_open
        self._zoom = 1.0
        return self

    def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
        return True

    def application_openFiles_(self, sender, paths):
        """Dropping a PDF on the Dock icon, or opening one with this app."""
        for p in paths:
            if str(p).lower().endswith(".pdf") and self._on_open:
                self._on_open(str(p))
                break
        NSApp().replyToOpenOrPrint_(0)

    def openDocument_(self, sender):
        path = choose_pdf()
        if path and self._on_open:
            self._on_open(path)

    def reloadPage_(self, sender):
        wv = _state.get("webview")
        if wv is not None:
            wv.reload_(None)

    def _apply_zoom(self, value):
        self._zoom = max(0.5, min(2.5, value))
        wv = _state.get("webview")
        if wv is not None:
            try:
                wv.setPageZoom_(self._zoom)
            except Exception:
                pass

    def actualSize_(self, sender):
        self._apply_zoom(1.0)

    def zoomIn_(self, sender):
        self._apply_zoom(self._zoom + 0.1)

    def zoomOut_(self, sender):
        self._apply_zoom(self._zoom - 0.1)


def reload_window():
    wv = _state.get("webview")
    if wv is None:
        return
    on_main(lambda: wv.reload_(None), timeout=10)


# Fill the screen on launch. This is the screen's *visible* frame rather than
# macOS fullscreen: real fullscreen moves the app onto its own Space and hides the
# menu bar, which is disorienting for people who did not ask for it and fiddly to
# get out of. The usable area is the same bar the menu bar. Set this True for the
# immersive kind instead; the green button offers it either way.
START_FULLSCREEN = False


def run_window(url, on_open=None):
    """Open the window and hand control to the Cocoa run loop. Blocks."""
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    delegate = Delegate.alloc().initWithOnOpen_(on_open)
    _state["delegate"] = delegate
    app.setDelegate_(delegate)
    build_menus(delegate)

    frame = NSScreen.mainScreen().visibleFrame()
    rect = NSMakeRect(frame.origin.x, frame.origin.y,
                      frame.size.width, frame.size.height)
    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
             | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, NSBackingStoreBuffered, False)
    window.setTitle_(APP_NAME)
    window.setMinSize_((760, 560))
    # No setFrameAutosaveName here, deliberately. An autosaved frame is restored
    # again by makeKeyAndOrderFront_, *after* any setFrame call, so the remembered
    # size silently wins and the window opens at whatever size it was last left
    # at. Remembering the frame also contradicts opening filled every time.
    #
    # setFrame rather than the init contentRect: the content rect excludes the
    # title bar, so passing the visible frame there makes the window taller than
    # the screen and pushes its title bar out of reach.
    # Cocoa restores a window's geometry from saved application state once the
    # run loop starts, which lands *after* every call here and quietly undoes
    # this. Opting out of restoration is what actually makes the size stick.
    window.setRestorable_(False)
    window.setFrame_display_(rect, False)
    # Let the green button do proper fullscreen if the user wants it.
    window.setCollectionBehavior_(
        window.collectionBehavior() | NSWindowCollectionBehaviorFullScreenPrimary)

    config = WKWebViewConfiguration.alloc().init()
    webview = WKWebView.alloc().initWithFrame_configuration_(
        window.contentView().bounds(), config)
    webview.setAutoresizingMask_((1 << 1) | (1 << 4))       # width | height
    window.contentView().addSubview_(webview)
    _state["window"], _state["webview"] = window, webview

    webview.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
    window.makeKeyAndOrderFront_(None)
    if START_FULLSCREEN:
        window.toggleFullScreen_(None)
    app.activateIgnoringOtherApps_(True)
    app.run()


def terminate():
    """Quit from a worker thread, e.g. the UI's own Quit action."""
    try:
        on_main(lambda: NSApp().terminate_(None), timeout=5)
    except Exception:
        os._exit(0)
