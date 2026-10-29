// Java ImageIO shim. Policy: the PNG reader with ImageIO.read input settings; the
// BufferedImage's raster is written in its native band order and sample
// size, except IndexColorModel images which are expanded through the model.
import java.awt.image.BufferedImage;
import java.awt.image.ColorModel;
import java.awt.image.IndexColorModel;
import java.awt.image.Raster;
import java.io.BufferedOutputStream;
import java.io.DataOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import javax.imageio.ImageIO;
import javax.imageio.ImageReader;
import javax.imageio.stream.ImageInputStream;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Set;

public class ImageIOShim {
	private static String description(Throwable error) {
		StringBuilder result = new StringBuilder();
		Set<Throwable> seen = Collections.newSetFromMap(new IdentityHashMap<>());
		while (error != null && seen.add(error)) {
			if (result.length() != 0) result.append(" <- ");
			result.append(error.getClass().getSimpleName()).append(": ").append(error.getMessage());
			error = error.getCause();
		}
		return result.toString().replaceAll("\\R", " ");
	}

	public static void main(String[] args) {
		try { run(args); }
		catch (AssertionError error) { // the library returned without error but not an image the shim can write
			System.out.println("API " + description(error));
			System.exit(71);
		}
		catch (Throwable error) {
			System.out.println("SHIM " + description(error));
			System.exit(70);
		}
	}

	private static void run(String[] args) throws IOException {
		if (args.length == 1 && args[0].equals("--version")) {
			System.out.println("Java ImageIO (" + System.getProperty("java.vendor") + " " + Runtime.version() + ")");
			return;
		}
		if (args.length != 2) {
			System.err.println("usage: ImageIOShim in.png out.pam");
			System.exit(2);
		}
		BufferedImage img;
		try (ImageInputStream input = ImageIO.createImageInputStream(new File(args[0]))) {
			if (input == null) throw new IOException("cannot open input");
			var readers = ImageIO.getImageReaders(input);
			if (!readers.hasNext()) throw new IOException("no reader accepted the file");
			ImageReader reader = readers.next();
			try {
				reader.addIIOReadWarningListener((r, text) -> System.out.println("WARN " + text.replaceAll("\\R", " ")));
				reader.setInput(input, true, true);
				img = reader.read(0, reader.getDefaultReadParam());
			} finally { reader.dispose(); }
		} catch (Throwable e) {
			System.out.println("ERR " + description(e));
			System.exit(1);
			return;
		}
		if (img == null) {
			System.out.println("ERR ImageIO.read returned null (no reader accepted the file)");
			System.exit(1);
		}
		int w = img.getWidth(), h = img.getHeight();
		Raster r = img.getRaster();
		ColorModel cm = img.getColorModel();
		int bands = r.getNumBands();
		int bits = r.getSampleModel().getSampleSize(0);
		boolean indexed = cm instanceof IndexColorModel;
		System.out.println("INFO type=" + img.getType() + " bands=" + bands + " bits=" + bits + " cm="
			+ cm.getClass().getSimpleName() + " hasAlpha=" + cm.hasAlpha() + " premultiplied=" + cm.isAlphaPremultiplied());
		if (cm.isAlphaPremultiplied()) throw new AssertionError("PNG reader returned premultiplied samples");
		if (bits != 1 && bits != 2 && bits != 4 && bits != 8 && bits != 16)
			throw new AssertionError("unexpected sample depth " + bits);
		int channels, maxval;
		if (indexed) {
			channels = cm.hasAlpha() ? 4 : 3;
			maxval = 255;
		} else {
			if (bands < 1 || bands > 4) {
				throw new AssertionError("unexpected band count " + bands);
			}
			channels = bands;
			maxval = bits == 16 ? 65535 : (bits >= 8 ? 255 : (1 << bits) - 1);
		}
		String tt = new String[] {"GRAYSCALE", "GRAYSCALE_ALPHA", "RGB", "RGB_ALPHA"}[channels - 1];
		try (DataOutputStream out = new DataOutputStream(new BufferedOutputStream(new FileOutputStream(args[1]), 1 << 20))) {
			out.writeBytes("P7\nWIDTH " + w + "\nHEIGHT " + h + "\nDEPTH " + channels + "\nMAXVAL " + maxval + "\nTUPLTYPE " + tt + "\nENDHDR\n");
			int[] px = new int[bands];
			for (int y = 0; y < h; y++) {
				for (int x = 0; x < w; x++) {
					r.getPixel(x, y, px);
					if (indexed) {
						IndexColorModel icm = (IndexColorModel) cm;
						int i = px[0];
						out.writeByte(icm.getRed(i));
						out.writeByte(icm.getGreen(i));
						out.writeByte(icm.getBlue(i));
						if (channels == 4) out.writeByte(icm.getAlpha(i));
					} else {
						for (int b = 0; b < bands; b++) {
							if (maxval > 255) out.writeShort(px[b]); else out.writeByte(px[b]);
						}
					}
				}
			}
		}
		System.out.println("OK " + w + " " + h + " " + channels + " " + (maxval > 255 ? 16 : 8));
	}
}
