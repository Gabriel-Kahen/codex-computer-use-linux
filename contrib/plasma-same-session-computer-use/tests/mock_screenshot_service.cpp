#include <QCoreApplication>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusMetaType>
#include <QDBusUnixFileDescriptor>
#include <QDBusVirtualObject>
#include <QFile>
#include <QImage>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTimer>
#include <QVariantMap>

#include <sys/stat.h>
#include <unistd.h>

#include <utility>

class ScreenShotObject final : public QDBusVirtualObject {
public:
    explicit ScreenShotObject(QString tracePath)
        : m_tracePath(std::move(tracePath)) {}

    QString introspect(const QString &) const override {
        return QStringLiteral(
            "<interface name=\"org.kde.KWin.ScreenShot2\">"
            "<method name=\"CaptureWindow\">"
            "<arg direction=\"in\" type=\"s\"/>"
            "<arg direction=\"in\" type=\"a{sv}\"/>"
            "<arg direction=\"in\" type=\"h\"/>"
            "<arg direction=\"out\" type=\"a{sv}\"/>"
            "</method>"
            "</interface>");
    }

    bool handleMessage(const QDBusMessage &message, const QDBusConnection &connection) override {
        if (message.interface() != QStringLiteral("org.kde.KWin.ScreenShot2")
            || message.member() != QStringLiteral("CaptureWindow") || message.arguments().size() != 3) {
            return false;
        }

        const QString handle = message.arguments().at(0).toString();
        const QVariantMap options = qdbus_cast<QVariantMap>(message.arguments().at(1));
        const auto descriptor = qvariant_cast<QDBusUnixFileDescriptor>(message.arguments().at(2));
        struct stat descriptorStat {};
        const bool pipeFile = descriptor.isValid()
            && fstat(descriptor.fileDescriptor(), &descriptorStat) == 0
            && S_ISFIFO(descriptorStat.st_mode);

        QImage image(2, 2, QImage::Format_ARGB32_Premultiplied);
        image.fill(qRgba(10, 20, 30, 255));
        const auto written = write(descriptor.fileDescriptor(), image.constBits(), image.sizeInBytes());

        QJsonObject trace {
            {QStringLiteral("handle"), handle},
            {QStringLiteral("includeDecoration"), options.value(QStringLiteral("include-decoration")).toBool()},
            {QStringLiteral("includeShadow"), options.value(QStringLiteral("include-shadow")).toBool()},
            {QStringLiteral("nativeResolution"), options.value(QStringLiteral("native-resolution")).toBool()},
            {QStringLiteral("pipeFileDescriptor"), pipeFile},
            {QStringLiteral("bytesWritten"), static_cast<qint64>(written)},
        };
        QFile traceFile(m_tracePath);
        if (!traceFile.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            return false;
        }
        traceFile.write(QJsonDocument(trace).toJson(QJsonDocument::Compact));
        traceFile.close();

        QVariantMap result;
        result.insert(QStringLiteral("type"), QStringLiteral("raw"));
        result.insert(QStringLiteral("width"), image.width());
        result.insert(QStringLiteral("height"), image.height());
        result.insert(QStringLiteral("stride"), image.bytesPerLine());
        result.insert(QStringLiteral("format"), static_cast<quint32>(image.format()));
        connection.send(message.createReply(QVariantList {result}));
        QTimer::singleShot(100, QCoreApplication::instance(), &QCoreApplication::quit);
        return true;
    }

private:
    QString m_tracePath;
};

int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);
    if (argc != 3) {
        return 2;
    }
    auto connection = QDBusConnection::sessionBus();
    if (!connection.registerService(QStringLiteral("org.kde.KWin.ScreenShot2"))) {
        return 1;
    }
    ScreenShotObject object(QString::fromLocal8Bit(argv[1]));
    if (!connection.registerVirtualObject(QStringLiteral("/org/kde/KWin/ScreenShot2"), &object)) {
        return 1;
    }
    QFile ready(QString::fromLocal8Bit(argv[2]));
    if (!ready.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return 1;
    }
    ready.write("ready\n");
    ready.close();
    return app.exec();
}
