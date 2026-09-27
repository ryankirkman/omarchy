// Measurement-only QML plugin. Never loaded by the installed picker.
#include <QCoreApplication>
#include <QKeyEvent>
#include <QImage>
#include <QQuickWindow>
#include <QSGRendererInterface>
#include <QSet>
#include <QQmlExtensionPlugin>
#include <QWindow>
#include <QTimer>
#include <QOpenGLContext>
#include <QOpenGLExtraFunctions>
#include <linux/perf_event.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <cerrno>
#include <cstring>
#include <atomic>
#include <chrono>
#include <memory>
#include <mutex>
#include <time.h>
#include <qqml.h>

class Probe : public QObject {
  Q_OBJECT
  std::atomic<double> animated{0}, synchronized{0}, rendering{0}, rendered{0};
  QList<QMetaObject::Connection> connections;
  QTimer timer;
  mutable std::mutex frameMutex;
  QVariantMap completedFrame;
  struct GpuState {
    struct Pending { GLuint id; double beforeSyncAt; };
    GLuint active = 0;
    double activeSync = 0;
    QList<Pending> pending;
    std::mutex mutex;
    QString renderer;
    QVariantList samples;
  };
  std::shared_ptr<GpuState> gpu;
  int instructionFd = -1;
  struct CpuCounter { const char *name; quint64 event; int fd = -1; };
  QList<CpuCounter> cpuCounters;
  std::atomic<int> releasedGraphs{0};
  QMetaObject::Connection resourceConnection;
public:
  explicit Probe(QObject *parent = nullptr) : QObject(parent) {
    timer.setSingleShot(true);
    timer.setTimerType(Qt::PreciseTimer);
    connect(&timer, &QTimer::timeout, this, &Probe::tick);
  }
  ~Probe() override {
    for (const auto &counter : cpuCounters) if (counter.fd >= 0) ::close(counter.fd);
    if (instructionFd >= 0) ::close(instructionFd);
  }
  Q_INVOKABLE void trackResourceRelease(QObject *target, bool aggressive) {
    auto *window = qobject_cast<QQuickWindow *>(target);
    if (!window) qFatal("Resource test target must be a QQuickWindow");
    QObject::disconnect(resourceConnection);
    resourceConnection = connect(window, &QQuickWindow::sceneGraphInvalidated, this,
      [this] { ++releasedGraphs; }, Qt::DirectConnection);
    if (aggressive) {
      window->setPersistentGraphics(false);
      window->setPersistentSceneGraph(false);
    }
  }
  Q_INVOKABLE int resourceReleaseCount() const { return releasedGraphs.load(); }
  Q_INVOKABLE QString enableInstructions() {
    if (instructionFd >= 0) return {};
    perf_event_attr attributes{};
    attributes.type = PERF_TYPE_HARDWARE;
    attributes.size = sizeof(attributes);
    attributes.config = PERF_COUNT_HW_INSTRUCTIONS;
    attributes.exclude_kernel = 1;
    attributes.exclude_hv = 1;
    attributes.read_format = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
    instructionFd = int(syscall(SYS_perf_event_open, &attributes, 0, -1, -1, PERF_FLAG_FD_CLOEXEC));
    return instructionFd >= 0 ? QString() : QString::fromLocal8Bit(std::strerror(errno));
  }
  Q_INVOKABLE QVariantMap instructions() const {
    struct { quint64 count, enabled, running; } value{};
    if (instructionFd < 0 || ::read(instructionFd, &value, sizeof(value)) != sizeof(value))
      return {{"error", QString::fromLocal8Bit(std::strerror(errno))}};
    // This event belongs only to the GUI thread. Renderer/kernel work is excluded.
    return {{"count", double(value.count)}, {"enabled", double(value.enabled)}, {"running", double(value.running)}};
  }
  Q_INVOKABLE QString enableCpuProfile() {
    if (!cpuCounters.isEmpty()) return QStringLiteral("CPU profile already enabled");
    const auto error = enableInstructions();
    if (!error.isEmpty()) return error;
    cpuCounters = {{"cycles", PERF_COUNT_HW_CPU_CYCLES},
                   {"branches", PERF_COUNT_HW_BRANCH_INSTRUCTIONS},
                   {"branchMisses", PERF_COUNT_HW_BRANCH_MISSES},
                   {"cacheMisses", PERF_COUNT_HW_CACHE_MISSES}};
    for (auto &counter : cpuCounters) {
      perf_event_attr attributes{};
      attributes.type = PERF_TYPE_HARDWARE;
      attributes.size = sizeof(attributes);
      attributes.config = counter.event;
      attributes.exclude_kernel = 1;
      attributes.exclude_hv = 1;
      attributes.read_format = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
      // Same GUI-thread group as instructions, so all events schedule together.
      counter.fd = int(syscall(SYS_perf_event_open, &attributes, 0, -1, instructionFd, PERF_FLAG_FD_CLOEXEC));
      if (counter.fd < 0) return QString::fromLatin1(counter.name) + ": " + QString::fromLocal8Bit(std::strerror(errno));
    }
    return {};
  }
  Q_INVOKABLE QVariantMap cpuProfile() const {
    QVariantMap result;
    for (const auto &counter : cpuCounters) {
      struct { quint64 count, enabled, running; } value{};
      if (counter.fd < 0 || ::read(counter.fd, &value, sizeof(value)) != sizeof(value))
        return {{"error", QString::fromLocal8Bit(std::strerror(errno))}};
      result.insert(QString::fromLatin1(counter.name), QVariantMap{{"count", double(value.count)},
        {"enabled", double(value.enabled)}, {"running", double(value.running)}});
    }
    return result;
  }
  Q_INVOKABLE void schedule(int ms) { timer.start(ms); }
  Q_INVOKABLE void stop() { timer.stop(); }
  Q_INVOKABLE double cpuNow() const {
    timespec stamp{};
    if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &stamp)) qFatal("Cannot read process CPU clock");
    return stamp.tv_sec * 1000.0 + stamp.tv_nsec / 1e6;
  }
  Q_INVOKABLE double now() const {
    using namespace std::chrono;
    return duration<double, std::milli>(steady_clock::now().time_since_epoch()).count();
  }
  Q_INVOKABLE void key(QObject *target, int key, const QString &text) {
    auto *window = qobject_cast<QWindow *>(target);
    if (!window) qFatal("Benchmark key target must be a QWindow");
    QCoreApplication::postEvent(window, new QKeyEvent(QEvent::KeyPress, key, Qt::NoModifier, text));
    QCoreApplication::postEvent(window, new QKeyEvent(QEvent::KeyRelease, key, Qt::NoModifier, text));
  }
  Q_INVOKABLE void watch(QObject *target, bool gpuTiming) {
    auto *window = qobject_cast<QQuickWindow *>(target);
    if (!window) qFatal("Benchmark watch target must be a QQuickWindow");
    for (const auto &connection : connections) QObject::disconnect(connection);
    connections.clear();
    gpu = std::make_shared<GpuState>();
    connections << connect(window, &QQuickWindow::afterAnimating, this, [this] { animated = now(); }, Qt::DirectConnection);
    connections << connect(window, &QQuickWindow::beforeSynchronizing, this, [this] { synchronized = now(); }, Qt::DirectConnection);
    connections << connect(window, &QQuickWindow::beforeRendering, this, [this] { rendering = now(); }, Qt::DirectConnection);
    connections << connect(window, &QQuickWindow::afterRendering, this, [this] { rendered = now(); }, Qt::DirectConnection);
    connections << connect(window, &QQuickWindow::frameSwapped, this, [this] {
      const double completedAt = now();
      std::lock_guard<std::mutex> guard(frameMutex);
      completedFrame = {{"beforeSyncAt", synchronized.load()}, {"frameAt", completedAt},
                        {"sceneSyncMs", rendering - synchronized}, {"renderCpuMs", rendered - rendering}};
    }, Qt::DirectConnection);
    if (gpuTiming) {
      auto state = gpu;
      // Qt 6 records rendering commands before submitting them. End the
      // interval afterFrameEnd, not afterRendering, so it includes the clear
      // and submitted draws. Submission gaps are included in this interval.
      connections << connect(window, &QQuickWindow::beforeRendering, this, [this, state] {
        auto *context = QOpenGLContext::currentContext();
        if (!context) return;
        auto *gl = context->extraFunctions();
        const auto getResult64 = reinterpret_cast<PFNGLGETQUERYOBJECTUI64VPROC>(context->getProcAddress("glGetQueryObjectui64v"));
        if (!getResult64 || !context->hasExtension("GL_ARB_timer_query")) return;
        {
          std::lock_guard<std::mutex> guard(state->mutex);
          if (state->renderer.isEmpty()) state->renderer = QString::fromLatin1(reinterpret_cast<const char *>(gl->glGetString(GL_RENDERER)));
        }
        // Poll only completed queries; never block the renderer for a result.
        while (!state->pending.isEmpty()) {
          const auto query = state->pending.first();
          GLuint available = 0;
          gl->glGetQueryObjectuiv(query.id, GL_QUERY_RESULT_AVAILABLE, &available);
          if (!available) break;
          GLuint64 nanos = 0;
          getResult64(query.id, GL_QUERY_RESULT, &nanos);
          gl->glDeleteQueries(1, &query.id);
          state->pending.removeFirst();
          std::lock_guard<std::mutex> guard(state->mutex);
          state->samples.append(QVariantMap{{"beforeSyncAt", query.beforeSyncAt}, {"gpuMs", nanos / 1e6}});
        }
        GLint existing = 0;
        gl->glGetQueryiv(GL_TIME_ELAPSED, GL_CURRENT_QUERY, &existing);
        if (existing) return; // Do not interfere with another profiler.
        gl->glGenQueries(1, &state->active);
        state->activeSync = synchronized.load();
        gl->glBeginQuery(GL_TIME_ELAPSED, state->active);
      }, Qt::DirectConnection);
      connections << connect(window, &QQuickWindow::afterFrameEnd, this, [state] {
        if (!state->active) return;
        auto *context = QOpenGLContext::currentContext();
        if (!context) return;
        auto *gl = context->extraFunctions();
        gl->glEndQuery(GL_TIME_ELAPSED);
        state->pending.append({state->active, state->activeSync});
        state->active = 0;
      }, Qt::DirectConnection);
      connections << connect(window, &QQuickWindow::sceneGraphInvalidated, this, [state] {
        auto *context = QOpenGLContext::currentContext();
        if (!context) return; // Context destruction itself releases GL objects.
        auto *gl = context->extraFunctions();
        const auto getResult64 = reinterpret_cast<PFNGLGETQUERYOBJECTUI64VPROC>(context->getProcAddress("glGetQueryObjectui64v"));
        if (!getResult64 || !context->hasExtension("GL_ARB_timer_query")) return;
        for (const auto &query : state->pending) gl->glDeleteQueries(1, &query.id);
        state->pending.clear();
      }, Qt::DirectConnection);
    }
  }
  Q_INVOKABLE QVariantMap gpuStats() const {
    if (!gpu) return {};
    std::lock_guard<std::mutex> guard(gpu->mutex);
    return {{"renderer", gpu->renderer}, {"samples", gpu->samples}};
  }
  Q_INVOKABLE QVariantMap windowInfo(QObject *target) const {
    auto *window = qobject_cast<QQuickWindow *>(target);
    if (!window) return {};
    const auto api = window->rendererInterface()->graphicsApi();
    const QString name = api == QSGRendererInterface::OpenGL ? "OpenGL" : api == QSGRendererInterface::Software ? "software" : api == QSGRendererInterface::Vulkan ? "Vulkan" : "other";
    return {{"width", window->width()}, {"height", window->height()}, {"dpr", window->devicePixelRatio()}, {"graphicsApi", name}};
  }
  Q_INVOKABLE QVariantMap frameStats() const {
    std::lock_guard<std::mutex> guard(frameMutex);
    return completedFrame;
  }
  Q_INVOKABLE bool capture(QObject *target, const QString &path) {
    auto *window = qobject_cast<QQuickWindow *>(target);
    return window && window->grabWindow().save(path);
  }
  Q_INVOKABLE QVariantMap imageStats(QObject *target) {
    auto *window = qobject_cast<QQuickWindow *>(target);
    const QImage image = window ? window->grabWindow() : QImage();
    QSet<QRgb> colors;
    // A cheap untimed guard against apparently valid frames that are blank.
    for (int y = 0; y < image.height() && colors.size() < 16; y += qMax(1, image.height() / 100))
      for (int x = 0; x < image.width() && colors.size() < 16; x += qMax(1, image.width() / 200))
        colors.insert(image.pixel(x, y));
    return {{"width", image.width()}, {"height", image.height()}, {"sampledColors", colors.size()}};
  }
signals:
  void tick();
};

class ProbePlugin : public QQmlExtensionPlugin {
  Q_OBJECT
  Q_PLUGIN_METADATA(IID QQmlExtensionInterface_iid)
public:
  void registerTypes(const char *uri) override { qmlRegisterType<Probe>(uri, 1, 0, "Probe"); }
};

#include "probe.moc"
