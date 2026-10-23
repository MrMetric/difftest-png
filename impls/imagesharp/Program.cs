// ImageSharp shim. Policy: Image.Identify for the IHDR bit depth, then
// Image.Load<Rgba32> (files up to 8 bits) or Image.Load<Rgba64> (16-bit),
// so no sample is scaled by the shim. If Identify itself throws (it does on
// APNG with fdAT chunks in 3.1.x), that is reported as WARN and the image is
// loaded as Rgba64, which ImageSharp upscales exactly (x257) for 8-bit data.
// Decoder options are ImageSharp's defaults.
using System;
using System.IO;
using System.Text;
using SixLabors.ImageSharp;
using SixLabors.ImageSharp.Formats.Png;
using SixLabors.ImageSharp.PixelFormats;

if (args.Length == 1 && args[0] == "--version")
{
	Console.WriteLine($"ImageSharp {typeof(Image).Assembly.GetCustomAttributes(typeof(System.Reflection.AssemblyInformationalVersionAttribute), false).OfType<System.Reflection.AssemblyInformationalVersionAttribute>().First().InformationalVersion.Split('+')[0]} (.NET {Environment.Version})");
	return 0;
}
if (args.Length != 2)
{
	Console.Error.WriteLine("usage: ImageSharpShim in.png out.pam");
	return 2;
}
try
{
	bool is16 = true;
	try
	{
		ImageInfo info = Image.Identify(args[0]);
		PngMetadata meta = info.Metadata.GetPngMetadata();
		is16 = meta.BitDepth == PngBitDepth.Bit16;
		Console.WriteLine($"INFO ihdr width={info.Width} height={info.Height} bit_depth={meta.BitDepth} color_type={meta.ColorType} interlace={meta.InterlaceMethod} bpp={info.PixelType.BitsPerPixel}");
	}
	catch (Exception e)
	{
		Console.WriteLine($"WARN Identify failed: {e.GetType().Name}: {e.Message}; loading as Rgba64");
	}
	using var fs = new FileStream(args[1], FileMode.Create, FileAccess.Write, FileShare.None, 1 << 20);
	int w, h;
	if (is16)
	{
		using Image<Rgba64> img = Image.Load<Rgba64>(args[0]);
		w = img.Width; h = img.Height;
		fs.Write(Encoding.ASCII.GetBytes($"P7\nWIDTH {w}\nHEIGHT {h}\nDEPTH 4\nMAXVAL 65535\nTUPLTYPE RGB_ALPHA\nENDHDR\n"));
		byte[] buf = new byte[w * 8];
		img.ProcessPixelRows(acc =>
		{
			for (int y = 0; y < acc.Height; y++)
			{
				Span<Rgba64> row = acc.GetRowSpan(y);
				for (int x = 0; x < row.Length; x++)
				{
					Rgba64 p = row[x];
					buf[8 * x] = (byte)(p.R >> 8); buf[8 * x + 1] = (byte)p.R;
					buf[8 * x + 2] = (byte)(p.G >> 8); buf[8 * x + 3] = (byte)p.G;
					buf[8 * x + 4] = (byte)(p.B >> 8); buf[8 * x + 5] = (byte)p.B;
					buf[8 * x + 6] = (byte)(p.A >> 8); buf[8 * x + 7] = (byte)p.A;
				}
				fs.Write(buf, 0, buf.Length);
			}
		});
		Console.WriteLine($"OK {w} {h} 4 16");
	}
	else
	{
		using Image<Rgba32> img = Image.Load<Rgba32>(args[0]);
		w = img.Width; h = img.Height;
		fs.Write(Encoding.ASCII.GetBytes($"P7\nWIDTH {w}\nHEIGHT {h}\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n"));
		byte[] buf = new byte[w * 4];
		img.ProcessPixelRows(acc =>
		{
			for (int y = 0; y < acc.Height; y++)
			{
				Span<Rgba32> row = acc.GetRowSpan(y);
				for (int x = 0; x < row.Length; x++)
				{
					Rgba32 p = row[x];
					buf[4 * x] = p.R; buf[4 * x + 1] = p.G; buf[4 * x + 2] = p.B; buf[4 * x + 3] = p.A;
				}
				fs.Write(buf, 0, buf.Length);
			}
		});
		Console.WriteLine($"OK {w} {h} 4 8");
	}
	return 0;
}
catch (Exception e)
{
	Console.WriteLine($"ERR {e.GetType().Name}: {e.Message}");
	foreach (string line in (e.StackTrace ?? "").Split('\n')) { if (line.Contains("SixLabors")) Console.WriteLine("INFO at " + line.Trim()); }
	return 1;
}
