#include <X11/Xlib.h>
#include <X11/extensions/Xcomposite.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define WINDOW_WIDTH 640
#define WINDOW_HEIGHT 360

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

static int allocate_color(Display *display, int screen, const char *name, unsigned long *pixel) {
    XColor color, exact;
    if (!XAllocNamedColor(display, DefaultColormap(display, screen), name, &color, &exact)) {
        fprintf(stderr, "cannot allocate test color %s\n", name);
        return 0;
    }
    *pixel = color.pixel;
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
    unsigned long background, top_left, top_right, bottom_left, bottom_right;
    if (!allocate_color(display, screen, "#123456", &background) ||
        !allocate_color(display, screen, "#ff0000", &top_left) ||
        !allocate_color(display, screen, "#00ff00", &top_right) ||
        !allocate_color(display, screen, "#0000ff", &bottom_left) ||
        !allocate_color(display, screen, "#ffff00", &bottom_right)) {
        XCloseDisplay(display);
        return 1;
    }
    Window window = XCreateSimpleWindow(
        display, RootWindow(display, screen), 20, 20, WINDOW_WIDTH, WINDOW_HEIGHT, 0, background,
        background
    );
    Pixmap background_pixmap = XCreatePixmap(
        display, window, WINDOW_WIDTH, WINDOW_HEIGHT, (unsigned)DefaultDepth(display, screen)
    );
    GC gc = XCreateGC(display, background_pixmap, 0, NULL);
    XSetForeground(display, gc, background);
    XFillRectangle(display, background_pixmap, gc, 0, 0, WINDOW_WIDTH, WINDOW_HEIGHT);
    unsigned long corner_colors[] = {top_left, top_right, bottom_left, bottom_right};
    int corner_x[] = {0, WINDOW_WIDTH - 1, 0, WINDOW_WIDTH - 1};
    int corner_y[] = {0, 0, WINDOW_HEIGHT - 1, WINDOW_HEIGHT - 1};
    for (int corner = 0; corner < 4; corner++) {
        XSetForeground(display, gc, corner_colors[corner]);
        XDrawPoint(display, background_pixmap, gc, corner_x[corner], corner_y[corner]);
    }
    XSetWindowBackgroundPixmap(display, window, background_pixmap);
    XFreeGC(display, gc);
    XFreePixmap(display, background_pixmap);
    XStoreName(display, window, "Codex-X11-Native-Smoke");
    XMapWindow(display, window);
    XFlush(display);
    for (;;) pause();
}
