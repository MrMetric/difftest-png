//! zune-png shim: decode one PNG with zune-png defaults and emit PAM.
//!
//! Policy: default `DecoderOptions`; whatever colourspace and depth the
//! decoder hands back is written as-is (16-bit swapped to big-endian).
use std::fs::File;
use std::io::{BufWriter, Write};
use zune_core::bytestream::ZCursor;
use zune_core::result::DecodingResult;
use zune_png::PngDecoder;

fn fail(msg: &str) -> ! {
	println!("ERR {msg}");
	std::process::exit(1);
}

fn main() {
	let args: Vec<String> = std::env::args().collect();
	if args.len() != 3 {
		eprintln!("usage: zune-png-shim in.png out.pam");
		std::process::exit(2);
	}
	let data = std::fs::read(&args[1]).unwrap_or_else(|e| fail(&format!("read: {e}")));
	let mut dec = PngDecoder::new(ZCursor::new(&data));
	dec.decode_headers().unwrap_or_else(|e| fail(&format!("{e:?}")));
	let (w, h) = dec.dimensions().unwrap_or_else(|| fail("no dimensions"));
	let cs = dec.colorspace().unwrap_or_else(|| fail("no colorspace"));
	let depth = dec.depth().unwrap_or_else(|| fail("no depth"));
	println!("INFO width={w} height={h} colorspace={cs:?} depth={depth:?}");
	let result = dec.decode().unwrap_or_else(|e| fail(&format!("{e:?}")));
	let channels = cs.num_components();
	let tt = match channels {
		1 => "GRAYSCALE",
		2 => "GRAYSCALE_ALPHA",
		3 => "RGB",
		4 => "RGB_ALPHA",
		n => fail(&format!("unexpected channel count {n}")),
	};
	let (maxval, body): (u32, Vec<u8>) = match result {
		DecodingResult::U8(v) => (255, v),
		DecodingResult::U16(v) => (65535, v.iter().flat_map(|s| s.to_be_bytes()).collect()),
		_ => fail("unexpected result variant (not U8/U16)"),
	};
	let expected = w * h * channels * if maxval > 255 { 2 } else { 1 };
	if body.len() != expected {
		fail(&format!("decoded {} bytes, expected {expected}", body.len()));
	}
	let mut o = BufWriter::new(File::create(&args[2]).unwrap_or_else(|e| fail(&format!("create: {e}"))));
	write!(o, "P7\nWIDTH {w}\nHEIGHT {h}\nDEPTH {channels}\nMAXVAL {maxval}\nTUPLTYPE {tt}\nENDHDR\n")
		.and_then(|_| o.write_all(&body))
		.and_then(|_| o.flush())
		.unwrap_or_else(|e| fail(&format!("write: {e}")));
	println!("OK {w} {h} {channels} {}", if maxval > 255 { 16 } else { 8 });
}
