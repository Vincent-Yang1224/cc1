$d = [Console]::In.ReadToEnd() | ConvertFrom-Json
$line = "$($d.workspace.current_dir) | $($d.model.display_name)"
if ($d.context_window.remaining_percentage) {
    $p = [math]::Round($d.context_window.remaining_percentage)
    $line += " | Context: $p% remaining"
}
Write-Output $line
