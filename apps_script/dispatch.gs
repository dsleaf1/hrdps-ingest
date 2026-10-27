/**
 * inReach wind bot: fast trigger.
 * Runs inside the bot's Gmail account (hrdpsforecast@gmail.com) every minute. When an unread email
 * newer than the last one it saw arrives, it starts the GitHub "inReach wind bot" workflow, which
 * reads the inbox, replies and marks the email read. GitHub's own 10-minute schedule stays on as a
 * backup. Needs one Script Property: GITHUB_TOKEN (fine-grained token, hrdps-ingest only,
 * Actions: Read and write).
 */
const REPO = 'dsleaf1/hrdps-ingest';
const WORKFLOW = 'inreach.yml';

function checkForRequests() {
  const props = PropertiesService.getScriptProperties();
  const last = Number(props.getProperty('LAST_SEEN_MS') || 0);
  let newest = 0;
  GmailApp.search('is:unread newer_than:2d', 0, 30).forEach(function (t) {
    t.getMessages().forEach(function (m) {
      if (m.isUnread()) newest = Math.max(newest, m.getDate().getTime());
    });
  });
  if (newest <= last) return;                       // nothing new since the last dispatch
  const r = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + REPO + '/actions/workflows/' + WORKFLOW + '/dispatches', {
      method: 'post',
      contentType: 'application/json',
      headers: {
        Authorization: 'Bearer ' + props.getProperty('GITHUB_TOKEN'),
        Accept: 'application/vnd.github+json'
      },
      payload: JSON.stringify({ ref: 'main' }),
      muteHttpExceptions: true
    });
  if (r.getResponseCode() === 204) {
    props.setProperty('LAST_SEEN_MS', String(newest));
  } else {
    console.error('GitHub dispatch failed: ' + r.getResponseCode() + ' ' + r.getContentText());
  }
}

/** Run once by hand: creates the every-minute trigger (and removes any old copies). */
function install() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('checkForRequests').timeBased().everyMinutes(1).create();
  checkForRequests();
}
