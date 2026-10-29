// ImageSharp shim. Policy: Image.Load into the library's native pixel type, rows
// converted to Rgba64 by the library and written at the IHDR precision. Decoder
// options are ImageSharp's defaults, so an APNG's frames are all decoded.
using System;
using System.IO;
using System.Text;
using SixLabors.ImageSharp;
using SixLabors.ImageSharp.Advanced;
using SixLabors.ImageSharp.Formats.Png;
using SixLabors.ImageSharp.PixelFormats;

if (args.Length == 1 && args[0] == "--version")
{
	Console.WriteLine($"ImageSharp {typeof(Image).Assembly.GetCustomAttributes(typeof(System.Reflection.AssemblyInformationalVersionAttribute), false).OfType<System.Reflection.AssemblyInformationalVersionAttribute>().First().InformationalVersion.Split('+')[0]} (.NET {Environment.Version})");
	return 0;
}
if (args.Length != 2) { Console.WriteLine("SHIM usage: ImageSharpShim in.png out.pam"); return 70; }
Image img;
try { img = Image.Load(args[0]); }
catch (Exception e) { Console.WriteLine("ERR " + Describe(e)); return 1; }
using (img)
{
	try
	{
		// ProcessPixelRows visits the root frame, the default image.
		Console.WriteLine($"INFO type={img.GetType().Name} frames={img.Frames.Count} bits={img.Metadata.GetPngMetadata().BitDepth}");
		img.AcceptVisitor(new PamWriter(args[1]));
	}
	catch (ApiException e) { Console.WriteLine("API " + e.Message); return 71; }
	catch (Exception e) { Console.WriteLine("SHIM " + Describe(e)); return 70; }
}
return 0;

static string Describe(Exception error)
{
	var causes = new List<string>();
	for (Exception? e = error; e != null; e = e.InnerException)
		causes.Add($"{e.GetType().Name}: {e.Message}");
	return string.Join(" <- ", causes).Replace('\n', ' ').Replace('\r', ' ');
}

// The library returned without error but not an image the shim can write: its result.
class ApiException(string message) : Exception(message) { }

class PamWriter(string path) : IImageVisitor
{
	public void Visit<TPixel>(Image<TPixel> img) where TPixel : unmanaged, IPixel<TPixel>
	{
		bool wide = img.Metadata.GetPngMetadata().BitDepth == PngBitDepth.Bit16;
		int w = img.Width, h = img.Height, maxval = wide ? 65535 : 255;
		if (w <= 0 || h <= 0) throw new ApiException($"invalid image dimensions {w}x{h}");
		using var output = File.Create(path);
		output.Write(Encoding.ASCII.GetBytes($"P7\nWIDTH {w}\nHEIGHT {h}\nDEPTH 4\nMAXVAL {maxval}\nTUPLTYPE RGB_ALPHA\nENDHDR\n"));
		var rgba = new Rgba64[w];
		var bytes = new byte[checked(w * (wide ? 8 : 4))];
		img.ProcessPixelRows(rows =>
		{
			for (int y = 0; y < rows.Height; y++)
			{
				PixelOperations<TPixel>.Instance.ToRgba64(img.Configuration, rows.GetRowSpan(y), rgba);
				for (int x = 0; x < w; x++)
				{
					var p = rgba[x];
					if (!wide)
					{
						if (p.R % 257 != 0 || p.G % 257 != 0 || p.B % 257 != 0 || p.A % 257 != 0)
							throw new ApiException("8-bit PNG yielded non-8-bit samples");
						int i = x * 4;
						bytes[i] = (byte)(p.R / 257); bytes[i + 1] = (byte)(p.G / 257);
						bytes[i + 2] = (byte)(p.B / 257); bytes[i + 3] = (byte)(p.A / 257);
					}
					else
					{
						int i = x * 8;
						bytes[i] = (byte)(p.R >> 8); bytes[i + 1] = (byte)p.R;
						bytes[i + 2] = (byte)(p.G >> 8); bytes[i + 3] = (byte)p.G;
						bytes[i + 4] = (byte)(p.B >> 8); bytes[i + 5] = (byte)p.B;
						bytes[i + 6] = (byte)(p.A >> 8); bytes[i + 7] = (byte)p.A;
					}
				}
				output.Write(bytes);
			}
		});
		output.Flush();
		Console.WriteLine($"OK {w} {h} 4 {(wide ? 16 : 8)}");
	}
}
