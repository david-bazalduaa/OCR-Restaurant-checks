from pathlib import Path
import pprint
import sys

from src.ocr_parser import ocr_and_parse


def main():
    if len(sys.argv) < 2:
        print("Uso: python -m src.dev_test ruta_imagen_1 [ruta_imagen_2 ...]")
        return

    for file_path in sys.argv[1:]:
        path = Path(file_path)
        print("=" * 90)
        print(path)
        result = ocr_and_parse(path.read_bytes())
        pprint.pp(result)
        print()


if __name__ == "__main__":
    main()