#include <QCoreApplication>
#include <QDBusArgument>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusUnixFileDescriptor>
#include <QFile>
#include <QImage>
#include <QVariantMap>

#include <iostream>
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
    QFile raw;
    if (!raw.open(pipeDescriptors[0], QIODevice::ReadOnly, QFileDevice::AutoCloseHandle)) {
        close(pipeDescriptors[0]);
        close(pipeDescriptors[1]);
        std::cerr << "failed to open capture pipe\n";
        return 1;
    }
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
    constexpr qint64 maxCaptureBytes = 256 * 1024 * 1024;
    if (width <= 0 || height <= 0 || stride <= 0 || expectedSize > maxCaptureBytes) {
        std::cerr << "KWin returned invalid capture metadata\n";
        return 1;
    }
    QByteArray bytes;
    bytes.reserve(expectedSize);
    while (bytes.size() < expectedSize) {
        const QByteArray chunk = raw.read(expectedSize - bytes.size());
        if (chunk.isEmpty()) {
            break;
        }
        bytes.append(chunk);
    }
    if (bytes.size() != expectedSize) {
        std::cerr << "KWin returned incomplete capture data\n";
        return 1;
    }
    QImage image(reinterpret_cast<const uchar *>(bytes.constData()), width, height, stride, format);
    if (!image.save(QString::fromLocal8Bit(argv[2]), "PNG")) {
        std::cerr << "failed to save PNG\n";
        return 1;
    }
    return 0;
}
