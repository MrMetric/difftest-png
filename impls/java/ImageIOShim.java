// Java ImageIO shim. Policy: ImageIO.read with the built-in PNG reader; the
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

public class ImageIOShim {
	public static void main(String[] args) throws IOException {
		if (args.length == 1 && args[0].equals("--version")) {
			System.out.println("Java ImageIO (" + System.getProperty("java.vendor") + " " + Runtime.version() + ")");
			return;
		}
		if (args.length != 2) {
			System.err.println("usage: ImageIOShim in.png out.pam");
			System.exit(2);
		}
		BufferedImage img;
		try {
			img = ImageIO.read(new File(args[0]));
		} catch (Throwable e) {
			System.out.println("ERR " + e.getClass().getSimpleName() + ": " + e.getMessage());
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
		int channels, maxval;
		if (indexed) {
			channels = cm.hasAlpha() ? 4 : 3;
			maxval = 255;
		} else {
			if (bands < 1 || bands > 4) {
				System.out.println("ERR unexpected band count " + bands);
				System.exit(1);
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
