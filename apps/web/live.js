/* ── LIVE TAB ────────────────────────────────────────────────── */

function renderLive() {
  const dict = t();
  const list = state.live || [];

  // ── group by creator ────────────────────────────────────────
  var groups = {};
  for (var i = 0; i < list.length; i++) {
    var room = list[i];
    var key = room.creator_key || ("_acc_" + room.account_id);
    if (!groups[key]) {
      groups[key] = { creator_key: key, display_name: room.display_name || key, rooms: [] };
    }
    groups[key].rooms.push(room);
  }

  // sort groups: recording first, then probing/online/offline/error;
  // within each tier sort by display_name alphabetically for stable ordering
  var groupOrder = { recording: 0, probing: 1, online: 1, offline: 1, error: 1 };
  var sortedGroups = Object.values(groups).sort(function (a, b) {
    var aBest = 9, bBest = 9;
    a.rooms.forEach(function (r) { var v = groupOrder[r.status] ?? 9; if (v < aBest) aBest = v; });
    b.rooms.forEach(function (r) { var v = groupOrder[r.status] ?? 9; if (v < bBest) bBest = v; });
    if (aBest !== bBest) return aBest - bBest;
    // secondary sort: alphabetically by display_name
    var aName = (a.display_name || "").toLowerCase();
    var bName = (b.display_name || "").toLowerCase();
    if (aName < bName) return -1;
    if (aName > bName) return 1;
    return 0;
  });

  const cards = document.getElementById("live-cards");
  cards.innerHTML = sortedGroups.length
    ? sortedGroups.map(function (g) {
        var roomsHtml = g.rooms.map(function (room) {
          var errHtml = room.error_message ? '<span class="live-error">' + escapeHtml(room.error_message) + "</span>" : "";
          return '<div class="live-sub ' + statusClass(room.status) + '">' +
            '<span class="status-dot ' + statusClass(room.status) + '"></span>' +
            '<span class="live-status-text">' + statusText(room.status) + "</span>" +
            '<span class="live-time">' + toLocalTime(room.updated_at) + "</span>" +
            errHtml +
            "</div>";
        }).join("");
        return '<div class="entity-card live-creator-card" data-creator="' + escapeHtml(g.creator_key) + '">' +
          '<div class="entity-top">' +
          '<h3 class="creator-name">' + escapeHtml(g.display_name) + "</h3>" +
          '<div class="live-room-count">' + (dict.labels.rooms || "\u76f4\u64ad\u95f4") + ": " + g.rooms.length + "</div>" +
          "</div>" +
          '<div class="live-rooms">' + roomsHtml + "</div>" +
          "</div>";
      }).join("")
    : '<div class="hint">' + dict.placeholders.noLive + "</div>";

  // ── stats ───────────────────────────────────────────────────
  const recording = list.filter((l) => l.status === "recording").length;
  const probing = list.filter((l) => l.status === "probing" || l.status === "online").length;
  const offline = list.filter((l) => l.status === "offline" || l.status === "error").length;
  const stats = [];
  stats.push({ label: dict.stats.liveRecording, value: recording });
  stats.push({ label: dict.stats.liveOnline, value: probing });
  stats.push({ label: dict.stats.liveOffline, value: offline });
  renderStatCards(document.getElementById("live-stats"), stats);
}