#include <QCoreApplication>
#include <QDBusArgument>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusUnixFileDescriptor>
#include <QImage>
#include <QVariantMap>

#include <array>
#include <cerrno>
#include <iostream>
#include <poll.h>
#include <thread>
#include <unistd.h>

int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("plasma-same-session-capture"));
    if (argc != 3) {
        std::cerr << "usage: plasma-same-session-capture WINDOW_UUID OUTPUT.png\n";
        return 2;
    }

    int pipeDescriptors[2];
    if (pipe(pipeDescriptors) != 0) {
        std::cerr << "failed to create capture pipe\n";
        return 1;
    }
    constexpr qint64 maxCaptureBytes = 256 * 1024 * 1024;
    QByteArray bytes;
    QString pipeError;
    std::thread reader([&] {
        std::array<char, 64 * 1024> chunk;
        qint64 totalBytes = 0;
        while (true) {
            pollfd descriptor = {pipeDescriptors[0], POLLIN, 0};
            int ready;
            do {
                ready = poll(&descriptor, 1, 25000);
            } while (ready < 0 && errno == EINTR);
            if (ready == 0) {
                pipeError = QStringLiteral("timed out reading KWin capture data");
                break;
            }
            if (ready < 0) {
                pipeError = QStringLiteral("failed to wait for KWin capture data");
                break;
            }
            const ssize_t count = read(pipeDescriptors[0], chunk.data(), chunk.size());
            if (count > 0) {
                totalBytes += count;
                if (totalBytes <= maxCaptureBytes) {
                    bytes.append(chunk.data(), count);
                } else if (pipeError.isEmpty()) {
                    pipeError = QStringLiteral("KWin capture data exceeds the safety limit");
                }
            } else if (count == 0) {
                break;
            } else if (errno != EINTR) {
                pipeError = QStringLiteral("failed to read KWin capture data");
                break;
            }
        }
        close(pipeDescriptors[0]);
    });
    QDBusMessage reply;
    {
        QDBusUnixFileDescriptor descriptor(pipeDescriptors[1]);
        close(pipeDescriptors[1]);
        QDBusMessage request = QDBusMessage::createMethodCall(
            QStringLiteral("org.kde.KWin.ScreenShot2"),
            QStringLiteral("/org/kde/KWin/ScreenShot2"),
            QStringLiteral("org.kde.KWin.ScreenShot2"),
            QStringLiteral("CaptureWindow"));
        QVariantMap options;
        options.insert(QStringLiteral("include-decoration"), false);
        options.insert(QStringLiteral("include-shadow"), false);
        options.insert(QStringLiteral("native-resolution"), true);
        request.setArguments({QString::fromLocal8Bit(argv[1]), options, QVariant::fromValue(descriptor)});
        reply = QDBusConnection::sessionBus().call(request, QDBus::Block, 20000);
    }
    reader.join();
    if (!pipeError.isEmpty()) {
        std::cerr << pipeError.toStdString() << "\n";
        return 1;
    }
    if (reply.type() == QDBusMessage::ErrorMessage) {
        std::cerr << reply.errorName().toStdString() << ": " << reply.errorMessage().toStdString() << "\n";
        return 1;
    }
    if (reply.arguments().isEmpty()) {
        std::cerr << "KWin returned no capture metadata\n";
        return 1;
    }
    const QVariantMap result = qdbus_cast<QVariantMap>(reply.arguments().constFirst());
    if (result.value(QStringLiteral("type")).toString() != QStringLiteral("raw")) {
        std::cerr << "KWin returned an unsupported capture format\n";
        return 1;
    }
    const int width = result.value(QStringLiteral("width")).toInt();
    const int height = result.value(QStringLiteral("height")).toInt();
    const int stride = result.value(QStringLiteral("stride")).toInt();
    const auto format = static_cast<QImage::Format>(result.value(QStringLiteral("format")).toUInt());
    const qint64 expectedSize = static_cast<qint64>(stride) * height;
    if (width <= 0 || height <= 0 || stride <= 0 || expectedSize > maxCaptureBytes) {
        std::cerr << "KWin returned invalid capture metadata\n";
        return 1;
    }
    if (bytes.size() != expectedSize) {
        std::cerr << "KWin returned incomplete capture data\n";
        return 1;
    }
    QImage image(reinterpret_cast<const uchar *>(bytes.constData()), width, height, stride, format);
    if (image.isNull() || !image.save(QString::fromLocal8Bit(argv[2]), "PNG")) {
        std::cerr << "failed to save PNG\n";
        return 1;
    }
    return 0;
}
