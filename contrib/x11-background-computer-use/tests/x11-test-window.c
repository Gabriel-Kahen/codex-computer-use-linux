#include <X11/Xlib.h>
#include <stdio.h>
#include <unistd.h>

int main(void) {
    Display *display = XOpenDisplay(NULL);
    if (!display) {
        fprintf(stderr, "cannot open DISPLAY\n");
        return 1;
    }
    int screen = DefaultScreen(display);
    XColor color, exact;
    if (!XAllocNamedColor(display, DefaultColormap(display, screen), "#123456", &color, &exact)) {
        fprintf(stderr, "cannot allocate test color\n");
        XCloseDisplay(display);
        return 1;
    }
    Window window = XCreateSimpleWindow(
        display, RootWindow(display, screen), 20, 20, 640, 360, 0, color.pixel, color.pixel
    );
    XStoreName(display, window, "Codex-X11-Native-Smoke");
    XMapWindow(display, window);
    XFlush(display);
    for (;;) pause();
}
