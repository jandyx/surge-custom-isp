// Signal only. Python owns locking, validation, atomic writes and conditional reload.
// No HTTP listener or credentials are needed for this local mailbox.
$persistentStore.write(JSON.stringify({event: $event.name, at: Date.now(), nonce: Math.random()}),
    "isp-sync-event");
$done();
