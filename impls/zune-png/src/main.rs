//! zune-png defaults, with row-bounded native-sample serialization.
use std::fs::File;
use std::io::{BufWriter, Write};
use zune_core::bytestream::ZCursor;
use zune_core::result::DecodingResult;
use zune_png::PngDecoder;

fn fail(msg: &str) -> ! { println!("ERR {msg}"); std::process::exit(1); }
fn shim_fail(msg: &str) -> ! { println!("SHIM {msg}"); std::process::exit(70); }
// The crate returned without error but not an image the shim can write: its result.
fn api_fail(msg: &str) -> ! { println!("API {msg}"); std::process::exit(71); }
fn write(out: &mut impl Write, bytes: &[u8]) {
	out.write_all(bytes).unwrap_or_else(|e| shim_fail(&format!("write: {e}")));
}
fn main() {
	let args: Vec<String> = std::env::args().collect();
	if args.len() != 3 { shim_fail("usage: zune-png-shim in.png out.pam"); }
	let data = std::fs::read(&args[1]).unwrap_or_else(|e| shim_fail(&format!("read: {e}")));
	let mut dec = PngDecoder::new(ZCursor::new(&data));
	dec.decode_headers().unwrap_or_else(|e| fail(&format!("{e:?}")));
	let (w, h) = dec.dimensions().unwrap_or_else(|| api_fail("no dimensions"));
	let cs = dec.colorspace().unwrap_or_else(|| api_fail("no colorspace"));
	let depth = dec.depth().unwrap_or_else(|| api_fail("no depth"));
	println!("INFO width={w} height={h} colorspace={cs:?} depth={depth:?}");
	let result = dec.decode().unwrap_or_else(|e| fail(&format!("{e:?}")));
	let channels = cs.num_components();
	let tt = match channels {
		1 => "GRAYSCALE", 2 => "GRAYSCALE_ALPHA", 3 => "RGB", 4 => "RGB_ALPHA",
		_ => api_fail("unexpected channel count"),
	};
	let maxval = match result { DecodingResult::U8(_) => 255, DecodingResult::U16(_) => 65535, _ => api_fail("unexpected result variant") };
	let row_samples = w.checked_mul(channels).filter(|&n| n > 0).unwrap_or_else(|| shim_fail("row size overflow"));
	let expected = row_samples.checked_mul(h).unwrap_or_else(|| shim_fail("image size overflow"));
	let mut out = BufWriter::new(File::create(&args[2]).unwrap_or_else(|e| shim_fail(&format!("create: {e}"))));
	write(&mut out, format!("P7\nWIDTH {w}\nHEIGHT {h}\nDEPTH {channels}\nMAXVAL {maxval}\nTUPLTYPE {tt}\nENDHDR\n").as_bytes());
	match result {
		DecodingResult::U8(v) => {
			if v.len() != expected { api_fail("decoded sample count differs from dimensions"); }
			for row in v.chunks_exact(row_samples) { write(&mut out, row); }
		}
		DecodingResult::U16(v) => {
			if v.len() != expected { api_fail("decoded sample count differs from dimensions"); }
			let mut bytes = vec![0u8; row_samples * 2];
			for row in v.chunks_exact(row_samples) {
				for (sample, dst) in row.iter().zip(bytes.chunks_exact_mut(2)) { dst.copy_from_slice(&sample.to_be_bytes()); }
				write(&mut out, &bytes);
			}
		}
		_ => unreachable!(),
	}
	out.flush().unwrap_or_else(|e| shim_fail(&format!("flush: {e}")));
	println!("OK {w} {h} {channels} {}", if maxval > 255 { 16 } else { 8 });
}
