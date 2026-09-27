import QtQuick
import Quickshell
import "Probe" as Bench
import "Fixture.js" as Fixture
import "Oracle.js" as Oracle

Scope {
  id: test
  Bench.Probe { id: clock; onTick: test.step() }
  property var cfg: Fixture.config
  property var prepared: cfg.variant === "custom" ? Oracle.prepare(cfg.options) : []
  property var samples: []
  property var expected: ({})
  property var actions: cfg.actions
  property var window: null
  property int iteration: 0
  property bool pending: false
  property bool failed: false
  property bool checkingImage: false
  property double startAt: 0
  property double cpuAt: 0
  property var instructionsAt: null
  property double blockCpuAt: 0
  property var blockInstructionsAt: null
  property int expectedIndex: 0
  property var imageCheck: ({})
  property var geometry: ({})

  function fail(message) {
    failed = true; pending = false; clock.stop()
    console.log("FAILURE " + message)
  }
  function stockRows(query) {
    var q = query.trim().toLowerCase(), rows = []
    for (var i = 0; i < cfg.options.length; i++) {
      var parts = cfg.options[i].split("\t")
      if (parts.length > 1) parts.shift()
      var label = parts.shift() || "", detail = parts.join("\t")
      if (!q || label.toLowerCase().indexOf(q) >= 0 || detail.toLowerCase().indexOf(q) >= 0)
        rows.push({label:label, detail:detail, value:detail ? label + "\t" + detail : label})
    }
    return rows
  }
  function oracleRows(query) {
    return cfg.variant !== "custom" ? stockRows(query) : Oracle.filterPrepared(prepared, Oracle.terms(query), true)
  }
  function verify(query) {
    var formatted = expected[query] || oracleRows(query)
    var wanted = formatted.map(r => ({label:r.label, detail:r.detail, value:r.value}))
    var actual = picker.item.benchmarkRows()
    if (JSON.stringify(actual) !== JSON.stringify(wanted)) {
      var first = 0
      while (first < Math.min(actual.length, wanted.length) && JSON.stringify(actual[first]) === JSON.stringify(wanted[first])) first++
      throw new Error("Rows differ for " + JSON.stringify(query) + ": counts " + actual.length + "/" + wanted.length
        + ", row " + first + ", actual=" + JSON.stringify(actual[first]) + ", expected=" + JSON.stringify(wanted[first]))
    }
    picker.item.benchmarkCheck(wanted, cfg.mode === "navigation")
    picker.item.benchmarkCheckHighlights(formatted)
  }
  function observe() {
    var next = picker.item.benchmarkWindow
    if (!next) { fail("No backing QQuickWindow"); return }
    if (window !== next) {
      window = next
      clock.watch(window, false)
      window.frameSwapped.connect(test.frame)
    }
    window.update()
  }
  function begin() {
    var queries = [""]
    for (var i = 0; i < actions.length; i++) if (actions[i].query !== undefined) queries.push(actions[i].query)
    for (var q of queries) expected[q] = oracleRows(q)
    picker.item.open(JSON.stringify({mode:"select",prompt:cfg.prompt,options:cfg.options,width:800,maxHeight:cfg.height}))
    Qt.callLater(function() {
      if (cfg.captureQuery) picker.item.setFilter(cfg.captureQuery)
      test.observe(); warmup.start()
    })
  }
  function step() {
    if (failed) return
    if (iteration === actions.length * (cfg.rounds + 1)) {
      var blockCpu = clock.cpuNow() - blockCpuAt
      var blockInstructions = clock.instructions().count - blockInstructionsAt.count
      var result = {variant:cfg.variant,samples:samples,
        measuredBlock:{cpuMs:blockCpu,guiInstructions:blockInstructions},
        imageCheck:imageCheck,window:clock.windowInfo(window),geometry:geometry,
        delegatesCreated:picker.item.benchmarkCreated,delegatesDestroyed:picker.item.benchmarkDestroyed}
      try {
        verify(picker.item.filterText)
        picker.item.setFilter("")
        var rows = oracleRows("")
        picker.item.activateIndex(rows.length - 1)
        if (picker.item.benchmarkSelection !== rows[rows.length - 1].value)
          throw new Error("Exact selection value differs")
      } catch (error) { fail(error); return }
      console.log("RESULT " + JSON.stringify(result))
      return
    }
    if (iteration === actions.length) {
      blockCpuAt = clock.cpuNow()
      blockInstructionsAt = clock.instructions()
      picker.item.benchmarkCreated = 0
      picker.item.benchmarkDestroyed = 0
    }
    var action = actions[iteration % actions.length]
    if (cfg.mode === "navigation")
      expectedIndex = (picker.item.selectedIndex + action.delta + cfg.options.length) % cfg.options.length
    instructionsAt = clock.instructions()
    cpuAt = clock.cpuNow()
    startAt = clock.now()
    pending = true
    clock.key(window, action.key, action.text || "")
  }
  function frame() {
    if (failed || !window) return
    var phases = clock.frameStats()
    if (!pending || picker.item.benchmarkInputAt < startAt || phases.beforeSyncAt < picker.item.benchmarkInputDone) return
    pending = false
    var done = clock.instructions()
    var action = actions[iteration % actions.length]
    var query = cfg.mode === "navigation" ? "" : action.query
    var sample = {query:query,action:action.name,frameMs:phases.frameAt-startAt,
      handlerMs:picker.item.benchmarkInputDone-picker.item.benchmarkInputAt,
      queueMs:picker.item.benchmarkInputAt-startAt,cpuMs:clock.cpuNow()-cpuAt,
      guiInstructions:done.count-instructionsAt.count,
      counterRunningFraction:(done.running-instructionsAt.running)/(done.enabled-instructionsAt.enabled)}
    if (!(sample.counterRunningFraction >= .99)) { fail("Instruction counter multiplexed"); return }
    sample.resultCount = picker.item.benchmarkCount()
    if (iteration >= actions.length) samples.push(sample)
    try {
      if (picker.item.filterText !== query) throw new Error("Wrong filter text")
      if (cfg.mode === "navigation" && picker.item.selectedIndex !== expectedIndex) throw new Error("Wrong selection index")
      if (iteration < actions.length) verify(query)
      else if (cfg.mode === "navigation") picker.item.benchmarkCheck([], true)
    } catch (error) { fail(error); return }
    iteration++
    clock.schedule([13,23,37,51][iteration % 4])
  }
  Loader {
    id: picker
    source: "Picker.qml"
    onLoaded: {
      item.benchmarkClock = clock
      var error = clock.enableInstructions()
      if (error) { test.fail(error); return }
      ready.start()
    }
    onStatusChanged: if (status === Loader.Error) test.fail("Picker failed to load")
  }
  Timer { id: ready; interval: 25; repeat: true; onTriggered: {
    if (picker.item.benchmarkReady) { stop(); settle.start() }
  } }
  Timer { id: settle; interval: 250; onTriggered: test.begin() }
  Timer { id: warmup; interval: 300; onTriggered: {
    try { test.verify(cfg.captureQuery || "") } catch (error) { test.fail(error); return }
    test.imageCheck = clock.imageStats(test.window)
    test.geometry = picker.item.benchmarkGeometry()
    if (test.imageCheck.sampledColors < 3) { test.fail("Blank initial window"); return }
    if (cfg.screenshot && !clock.capture(test.window,cfg.screenshot)) { test.fail("Screenshot failed"); return }
    if (cfg.captureQuery) picker.item.setFilter("")
    picker.item.benchmarkFocus()
    clock.schedule(23)
  } }
}
