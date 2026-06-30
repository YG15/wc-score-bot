// Paste this entire file into your Google Sheet's Apps Script editor.
// Extensions > Apps Script > delete everything > paste > Save > Deploy > New deployment > Web app > Anyone > Deploy
//
// This script receives data from daily_scores.py and writes it to the Sheet.

function doPost(e) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var data = JSON.parse(e.postData.contents);

  if (data.title) {
    ss.rename(data.title);
  }

  var payloads = data.sheets || (data.values ? [{ name: "Scores", values: data.values, frozen: 2 }] : []);
  var keep = {};

  payloads.forEach(function (p) {
    keep[p.name] = true;
    var sheet = ss.getSheetByName(p.name) || ss.insertSheet(p.name);
    sheet.clear();
    if (p.values && p.values.length) {
      var rng = sheet.getRange(1, 1, p.values.length, p.values[0].length);
      // Reset number format to General before writing. The Goals column reuses a
      // cell that previously held "Implied %" values, and clear() can leave the old
      // percent format behind - which made integer goal counts (e.g. 6) show as 600%.
      // Setting General first wipes that; "28.1%" strings still auto-format as percent.
      rng.setNumberFormat("General");
      rng.setValues(p.values);
      sheet.setFrozenRows(p.frozen || 1);
    }

    // Color the Points column for the Scores tab
    if (p.name === "Scores" && p.values.length > 2) {
      var headerRow = p.values[1];
      var pointsCol = headerRow.indexOf("Points") + 1; // 1-indexed; 0 if not found
      if (pointsCol > 0) {
        // Build a color array for all data rows (skip title row 1 and header row 2)
        // Last row is the TOTAL row - no color
        var dataRowCount = p.values.length - 3; // -2 headers -1 totals
        if (dataRowCount > 0) {
          var colors = [];
          for (var i = 2; i < p.values.length - 1; i++) {
            var pts = p.values[i][pointsCol - 1];
            // Group stage scores 3/1/0; knockout (R32+) scores 5/2/0.
            if (pts === 0)                  colors.push(["#F4CCCC"]); // red   - wrong
            else if (pts === 1 || pts === 2) colors.push(["#FFF2CC"]); // yellow - right direction
            else if (pts === 3 || pts === 5) colors.push(["#D9EAD3"]); // green  - exact score
            else                            colors.push([null]);       // unplayed - no color
          }
          sheet.getRange(3, pointsCol, colors.length, 1).setBackgrounds(colors);
        }
      }
    }
  });

  // Remove any tabs not in this update
  ss.getSheets().forEach(function (s) {
    if (!keep[s.getName()] && ss.getSheets().length > 1) {
      ss.deleteSheet(s);
    }
  });

  return ContentService
    .createTextOutput(JSON.stringify({ ok: true, tabs: payloads.length }))
    .setMimeType(ContentService.MimeType.JSON);
}
