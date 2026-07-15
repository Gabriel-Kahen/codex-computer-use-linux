#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/extensions/Xcomposite.h>
#include <X11/extensions/XRes.h>
#include <png.h>
#include <errno.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int x_error;
static const uint64_t MAX_CAPTURE_PIXELS = 7680ULL * 4320ULL;

static int record_x_error(Display *display, XErrorEvent *event) {
    (void)display;
    x_error = event->error_code;
    return 0;
}

static unsigned char component(unsigned long pixel, unsigned long mask) {
    if (!mask) return 0;
    unsigned shift = 0;
    while ((mask & 1UL) == 0) { mask >>= 1; shift++; }
    unsigned long value = (pixel >> shift) & mask;
    return (unsigned char)((value * 255UL + mask / 2UL) / mask);
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: %s XID OUTPUT.png | --pid XID\n", argv[0]);
        return 2;
    }
    errno = 0;
    char *end = NULL;
    const char *raw_xid = strcmp(argv[1], "--pid") == 0 ? argv[2] : argv[1];
    unsigned long xid = strtoul(raw_xid, &end, 0);
    if (errno || !end || *end || !xid) {
        fprintf(stderr, "invalid X11 window id\n");
        return 2;
    }
    Display *display = XOpenDisplay(NULL);
    if (!display) {
        fprintf(stderr, "cannot open DISPLAY\n");
        return 1;
    }
    if (strcmp(argv[1], "--pid") == 0) {
        XResClientIdSpec spec = {.client = xid, .mask = XRES_CLIENT_ID_PID_MASK};
        XResClientIdValue *ids = NULL;
        long count = 0;
        int event_base, error_base;
        int major, minor;
        if (!XResQueryExtension(display, &event_base, &error_base) ||
            !XResQueryVersion(display, &major, &minor) ||
            major < 1 || (major == 1 && minor < 2)) {
            fprintf(stderr, "XRes 1.2 client PID authentication is unavailable\n");
            XCloseDisplay(display);
            return 1;
        }
        if (XResQueryClientIds(display, 1, &spec, &count, &ids) != Success) {
            fprintf(stderr, "XRes client PID query failed\n");
            XCloseDisplay(display);
            return 1;
        }
        pid_t pid = -1;
        for (long index = 0; index < count; index++) {
            pid_t candidate = XResGetClientPid(&ids[index]);
            if (candidate > 0) pid = candidate;
        }
        XResClientIdsDestroy(count, ids);
        XCloseDisplay(display);
        if (pid <= 0) {
            fprintf(stderr, "XRes did not authenticate a PID for this window\n");
            return 1;
        }
        printf("%ld\n", (long)pid);
        return 0;
    }
    int event_base, error_base;
    if (!XCompositeQueryExtension(display, &event_base, &error_base)) {
        fprintf(stderr, "XComposite is unavailable\n");
        XCloseDisplay(display);
        return 1;
    }
    int screen = DefaultScreen(display);
    char selection[64];
    snprintf(selection, sizeof(selection), "_NET_WM_CM_S%d", screen);
    if (XGetSelectionOwner(display, XInternAtom(display, selection, False)) == None) {
        fprintf(stderr, "no X11 compositing manager is active; exact unobscured capture is unavailable\n");
        XCloseDisplay(display);
        return 1;
    }
    XWindowAttributes attributes;
    if (!XGetWindowAttributes(display, xid, &attributes) || attributes.map_state != IsViewable) {
        fprintf(stderr, "window is closed or not mapped; minimized windows cannot be captured exactly\n");
        XCloseDisplay(display);
        return 1;
    }
    if (!attributes.visual->red_mask || !attributes.visual->green_mask ||
        !attributes.visual->blue_mask) {
        fprintf(stderr, "window does not use a supported direct-color visual\n");
        XCloseDisplay(display);
        return 1;
    }
    XSetErrorHandler(record_x_error);
    Pixmap pixmap = XCompositeNameWindowPixmap(display, xid);
    XSync(display, False);
    if (x_error || !pixmap) {
        fprintf(stderr, "the compositor did not expose a named window pixmap\n");
        XCloseDisplay(display);
        return 1;
    }
    Window root;
    int x, y;
    unsigned width, height, border, depth;
    if (!XGetGeometry(display, pixmap, &root, &x, &y, &width, &height, &border, &depth)) {
        fprintf(stderr, "failed to read window pixmap geometry\n");
        XFreePixmap(display, pixmap);
        XCloseDisplay(display);
        return 1;
    }
    if (!width || !height || (uint64_t)width * height > MAX_CAPTURE_PIXELS) {
        fprintf(stderr, "window pixmap exceeds the 33,177,600-pixel capture budget\n");
        XFreePixmap(display, pixmap);
        XCloseDisplay(display);
        return 1;
    }
    XImage *image = XGetImage(display, pixmap, 0, 0, width, height, AllPlanes, ZPixmap);
    if (!image) {
        fprintf(stderr, "failed to read compositor window pixmap\n");
        XFreePixmap(display, pixmap);
        XCloseDisplay(display);
        return 1;
    }
    FILE *output = fopen(argv[2], "wb");
    if (!output) {
        perror("cannot create output");
        XDestroyImage(image);
        XFreePixmap(display, pixmap);
        XCloseDisplay(display);
        return 1;
    }
    png_structp png = png_create_write_struct(PNG_LIBPNG_VER_STRING, NULL, NULL, NULL);
    png_infop info = png ? png_create_info_struct(png) : NULL;
    if (!png || !info || setjmp(png_jmpbuf(png))) {
        fprintf(stderr, "failed to encode PNG\n");
        if (png) png_destroy_write_struct(&png, info ? &info : NULL);
        fclose(output);
        XDestroyImage(image);
        XFreePixmap(display, pixmap);
        XCloseDisplay(display);
        return 1;
    }
    png_init_io(png, output);
    png_set_IHDR(png, info, width, height, 8, PNG_COLOR_TYPE_RGB, PNG_INTERLACE_NONE,
                 PNG_COMPRESSION_TYPE_DEFAULT, PNG_FILTER_TYPE_DEFAULT);
    png_write_info(png, info);
    png_bytep row = malloc((size_t)width * 3);
    if (!row) png_error(png, "out of memory");
    for (unsigned py = 0; py < height; py++) {
        for (unsigned px = 0; px < width; px++) {
            unsigned long pixel = XGetPixel(image, px, py);
            row[px * 3] = component(pixel, attributes.visual->red_mask);
            row[px * 3 + 1] = component(pixel, attributes.visual->green_mask);
            row[px * 3 + 2] = component(pixel, attributes.visual->blue_mask);
        }
        png_write_row(png, row);
    }
    free(row);
    png_write_end(png, info);
    png_destroy_write_struct(&png, &info);
    fclose(output);
    XDestroyImage(image);
    XFreePixmap(display, pixmap);
    XCloseDisplay(display);
    return 0;
}
