with open('index.html', 'rb') as f:
    content = f.read()

# Replace broken emoji byte sequences with empty bytes
replacements = [
    (b'\xc3\xb0\xc5\xb8\xe2\x80\x9c\xc5\xa0 ', b''),  # chart emoji + space
    (b'\xc3\xb0\xc5\xb8\xe2\x80\x9c\xc5\xb8 ', b''),  # pin emoji + space
    (b'\xc3\xb0\xc5\xb8\xe2\x80\x9d\xc5\xbd', b''),   # search emoji (no space)
    (b'\xc3\xb0\xc5\xb8\xe2\x80\x9d\xc2\x8d ', b''),  # magnifier emoji + space
    (b'\xc3\xb0\xc5\xb8\xe2\x80\x99\xc2\xb0 ', b''),  # money emoji + space
    (b'\xc3\xb0\xc5\xb8\xe2\x80\x98\xe2\x80\xa0 ', b''),  # pointing emoji + space
    (b'\xc3\xb0\xc5\xb8\xe2\x80\x9c\xc2\x8d ', b''),  # pin emoji variant + space
    (b'\xc2\xb3 ', b''),  # hourglass + space
]

for old, new in replacements:
    if old in content:
        content = content.replace(old, new)
        print(f'Replaced: {repr(old)}')

with open('index.html', 'wb') as f:
    f.write(content)

print('Done fixing encoding')
