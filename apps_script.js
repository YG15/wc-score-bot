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
      sheet.getRange(1, 1, p.values.length, p.values[0].length).setValues(p.values);
      sheet.setFrozenRows(p.frozen || 1);
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
