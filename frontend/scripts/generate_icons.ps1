$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Drawing

$frontendDir = (Resolve-Path "$PSScriptRoot\..").Path
$repoDir = (Resolve-Path "$frontendDir\..").Path
$buildDir = Join-Path $repoDir ".tmp\release-assets"
New-Item -ItemType Directory -Path $buildDir -Force | Out-Null

function New-RoundedRectanglePath([float]$X, [float]$Y, [float]$Width, [float]$Height, [float]$Radius) {
    $path = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $diameter = $Radius * 2
    $path.AddArc($X, $Y, $diameter, $diameter, 180, 90)
    $path.AddArc($X + $Width - $diameter, $Y, $diameter, $diameter, 270, 90)
    $path.AddArc($X + $Width - $diameter, $Y + $Height - $diameter, $diameter, $diameter, 0, 90)
    $path.AddArc($X, $Y + $Height - $diameter, $diameter, $diameter, 90, 90)
    $path.CloseFigure()
    return $path
}

function New-AgentIconBitmap([int]$Size) {
    $bitmap = [System.Drawing.Bitmap]::new($Size, $Size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.Clear([System.Drawing.Color]::Transparent)
        $scale = $Size / 512.0
        $backgroundPath = New-RoundedRectanglePath (24 * $scale) (24 * $scale) (464 * $scale) (464 * $scale) (104 * $scale)
        try {
            $background = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(255, 28, 35, 40))
            try { $graphics.FillPath($background, $backgroundPath) } finally { $background.Dispose() }
        } finally {
            $backgroundPath.Dispose()
        }

        $mint = [System.Drawing.Color]::FromArgb(255, 101, 221, 176)
        $coral = [System.Drawing.Color]::FromArgb(255, 255, 126, 103)
        $lineWidth = [Math]::Max(2.0, 30 * $scale)
        $mintPen = [System.Drawing.Pen]::new($mint, $lineWidth)
        $coralPen = [System.Drawing.Pen]::new($coral, $lineWidth)
        try {
            $mintPen.StartCap = $mintPen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
            $coralPen.StartCap = $coralPen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
            $graphics.DrawLine($mintPen, 142 * $scale, 348 * $scale, 256 * $scale, 144 * $scale)
            $graphics.DrawLine($coralPen, 256 * $scale, 144 * $scale, 370 * $scale, 348 * $scale)
            $graphics.DrawLine($mintPen, 190 * $scale, 280 * $scale, 322 * $scale, 280 * $scale)
        } finally {
            $mintPen.Dispose()
            $coralPen.Dispose()
        }

        foreach ($node in @(
            @{ X = 142; Y = 348; Color = $mint },
            @{ X = 256; Y = 144; Color = $coral },
            @{ X = 370; Y = 348; Color = $coral }
        )) {
            $radius = 31 * $scale
            $brush = [System.Drawing.SolidBrush]::new($node.Color)
            try {
                $graphics.FillEllipse($brush, ($node.X * $scale) - $radius, ($node.Y * $scale) - $radius, $radius * 2, $radius * 2)
            } finally {
                $brush.Dispose()
            }
        }
    } finally {
        $graphics.Dispose()
    }
    return $bitmap
}

function Get-PngBytes([int]$Size) {
    $bitmap = New-AgentIconBitmap $Size
    $stream = [System.IO.MemoryStream]::new()
    try {
        $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
        return $stream.ToArray()
    } finally {
        $stream.Dispose()
        $bitmap.Dispose()
    }
}

$pngPath = Join-Path $buildDir "app-icon.png"
$pngBitmap = New-AgentIconBitmap 512
try { $pngBitmap.Save($pngPath, [System.Drawing.Imaging.ImageFormat]::Png) } finally { $pngBitmap.Dispose() }

$sizes = @(16, 24, 32, 48, 64, 128, 256)
$images = @($sizes | ForEach-Object { Get-PngBytes $_ })
$icoPath = Join-Path $buildDir "app-icon.ico"
$stream = [System.IO.File]::Open($icoPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
$writer = [System.IO.BinaryWriter]::new($stream)
try {
    $writer.Write([uint16]0)
    $writer.Write([uint16]1)
    $writer.Write([uint16]$sizes.Count)
    $offset = 6 + (16 * $sizes.Count)
    for ($index = 0; $index -lt $sizes.Count; $index++) {
        $size = $sizes[$index]
        $dimension = if ($size -eq 256) { 0 } else { $size }
        $writer.Write([byte]$dimension)
        $writer.Write([byte]$dimension)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]32)
        $writer.Write([uint32]$images[$index].Length)
        $writer.Write([uint32]$offset)
        $offset += $images[$index].Length
    }
    foreach ($image in $images) { $writer.Write($image) }
} finally {
    $writer.Dispose()
    $stream.Dispose()
}

Write-Output $pngPath
Write-Output $icoPath
