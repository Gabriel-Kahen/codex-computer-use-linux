#include <QCoreApplication>
#include <QDBusArgument>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusUnixFileDescriptor>
#include <QElapsedTimer>
#include <QFile>
#include <QImage>
#include <QTemporaryFile>
#include <QThread>
#include <QVariantMap>

#include <iostream>

int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("plasma-same-session-capture"));
    if (argc != 3) {
        std::cerr << "usage: plasma-same-session-capture WINDOW_UUID OUTPUT.png\n";
        return 2;
    }

    QTemporaryFile raw;
    if (!raw.open()) {
        std::cerr << "failed to create capture buffer\n";
        return 1;
    }
    QDBusUnixFileDescriptor descriptor(raw.handle());
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
    QDBusMessage reply = QDBusConnection::sessionBus().call(request, QDBus::Block, 20000);
    if (reply.type() == QDBusMessage::ErrorMessage) {
        std::cerr << reply.errorName().toStdString() << ": " << reply.errorMessage().toStdString() << "\n";
        return 1;
    }
    if (reply.arguments().isEmpty()) {
        std::cerr << "KWin returned no capture metadata\n";
        return 1;
    }
    const QVariantMap result = qdbus_cast<QVariantMap>(reply.arguments().constFirst());
    const int width = result.value(QStringLiteral("width")).toInt();
    const int height = result.value(QStringLiteral("height")).toInt();
    const int stride = result.value(QStringLiteral("stride")).toInt();
    const auto format = static_cast<QImage::Format>(result.value(QStringLiteral("format")).toUInt());
    const qint64 expectedSize = static_cast<qint64>(stride) * height;
    QElapsedTimer timer;
    timer.start();
    while (raw.size() < expectedSize && timer.elapsed() < 10000) {
        QThread::msleep(10);
    }
    raw.seek(0);
    const QByteArray bytes = raw.readAll();
    if (width <= 0 || height <= 0 || stride <= 0 || bytes.size() < expectedSize) {
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
