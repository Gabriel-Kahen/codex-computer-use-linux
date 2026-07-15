#include <X11/Xlib.h>
#include <X11/extensions/Xcomposite.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int x_error;

static int record_x_error(Display *display, XErrorEvent *event) {
    (void)display;
    x_error = event->error_code;
    return 0;
}

static int wait_for_compositor(Display *display, int screen) {
    XSetWindowAttributes attributes = {.override_redirect = True};
    Window window = XCreateWindow(
        display, RootWindow(display, screen), 0, 0, 1, 1, 0, CopyFromParent, InputOutput,
        CopyFromParent, CWOverrideRedirect, &attributes
    );
    XMapWindow(display, window);
    XSetErrorHandler(record_x_error);
    XSync(display, False);

    for (int attempt = 0; attempt < 100; attempt++) {
        x_error = 0;
        Pixmap pixmap = XCompositeNameWindowPixmap(display, window);
        XSync(display, False);
        if (!x_error && pixmap) {
            XFreePixmap(display, pixmap);
            XDestroyWindow(display, window);
            XCloseDisplay(display);
            return 0;
        }
        usleep(50000);
    }

    fprintf(stderr, "compositor did not redirect the probe window\n");
    XDestroyWindow(display, window);
    XCloseDisplay(display);
    return 1;
}

int main(int argc, char **argv) {
    Display *display = XOpenDisplay(NULL);
    if (!display) {
        fprintf(stderr, "cannot open DISPLAY\n");
        return 1;
    }
    int screen = DefaultScreen(display);
    if (argc == 2 && strcmp(argv[1], "--wait-for-compositor") == 0) {
        return wait_for_compositor(display, screen);
    }
    if (argc != 1) {
        fprintf(stderr, "usage: %s [--wait-for-compositor]\n", argv[0]);
        XCloseDisplay(display);
        return 2;
    }
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
