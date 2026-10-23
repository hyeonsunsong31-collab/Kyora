The quality gate measured iteration $iteration of "$title" at $pass_pct % ($passed of $total counted checks) against a threshold of $threshold_pct %. It did not pass.

Failing or missing checks:
$failing_list

$reviewer asks for these fixes:
$fix_instructions

Files it suspects: $suspected_files

Fix the game code so these checks pass. Keep every acceptance test id and do not weaken or delete tests; if a test itself is wrong, correct it to check the behaviour described in KYORA_TESTSPEC.md. Keep .kyora/manifest.json accurate. When done, stop and summarise what you changed.
