#!/usr/bin/env bash
# Derived from InterCode c3e46d8; see docs/benchmarks/intercode-setup-corrections.md.
set -euo pipefail
export TZ=UTC

rm -rf /workspace /backup

## directories
mkdir -p /backup
mkdir -p /workspace/dir1
mkdir -p /workspace/dir2/mysql
mkdir -p /workspace/test/dir1
mkdir -p /workspace/test/2dir
mkdir -p /workspace/test/3dir
mkdir -p -m 755 /workspace/test/1dir

## c files
echo -e '#include <stdio.h>\n\nint main() {\n\tprintf("Hello, World!");\n\treturn 0;\n}' > /workspace/dir1/hello.c
echo -e '#include <stdio.h>\n\nint main() {\n\tint a, b, sum;\n\tprintf("Enter two numbers to add\\n");\n\tscanf("%d%d", &a, &b);\n\tsum = a + b;\n\tprintf("Sum of entered numbers = %d\\n",sum);\n\treturn 0;\n}' > /workspace/dir1/sum.c

## shell scripts
echo -e '#!/bin/bash\n\n# This is a script file' > /workspace/dir1/script1.sh
echo -e '#!/bin/bash\n\n# New script file' > /workspace/dir1/new1.sh
echo -e '#!/bin/bash\n\n# New script file' > /workspace/new.sh
chmod +x /workspace/dir1/script1.sh /workspace/dir1/new1.sh /workspace/new.sh

## txt files
echo -e 'hello!' > /workspace/dir1/hello.txt
echo -e 'Another hello world file' > /workspace/dir2/foo.txt
echo -e 'hello!' > /workspace/dir2/hello.txt
echo -e 'The first line\nThe second line\nThe third line\nThe fourth line;\nThe fifth line' > /workspace/dir1/long.txt
echo -e 'The first line\nThe second line\nThe third line\nThe fourth line;\nThe fifth line\nTERMINATEThe sixth line--' > /workspace/dir1/terminate.txt
echo '/workspace/dir1' > /workspace/results.txt
echo '/workspace/dir2/foo.txt' > /workspace/files.txt
echo -e 'The first line' > /workspace/dir1/a.txt
# Avoid `yes | head` SIGPIPE under pipefail while preserving ten blank lines.
for _ in {1..10}; do echo >> /workspace/dir1/a.txt; done
echo -e 'The first line\nThe second line\nThe third line\n' > /workspace/dir1/file.txt
echo -e 'new file\n' > /backup/file.txt
touch /workspace/dir1/readonly.txt
chmod 400 /workspace/dir1/readonly.txt
touch /workspace/dir1/all.txt
chmod 777 /workspace/dir1/all.txt
dd if=/dev/zero of=/workspace/dir1/file.txt bs=2K count=2 status=none
dd if=/dev/zero of=/workspace/dir1/file.c bs=1K count=2 status=none

## hidden, SQL, and CSV files
echo -e 'This is a hidden file' > /workspace/dir1/.hidden1.txt
echo -e 'This is another hidden file' > /workspace/.hidden.txt
echo -e 'CREATE TABLE Persons (\nPersonID int,\nLastName varchar(255),\nFirstName varchar(255),\nAddress varchar(255),\nCity varchar(255)\n);' > /workspace/dir2/mysql/sql1.sql
echo -e 'column1,column2,column3\nvalue1,value2,value3\nvalue4,value5,value6\nvalue7,values8,values9' > /workspace/dir2/csvfile1.csv
: > /workspace/recent.txt
: > /workspace/recent1.txt
: > /workspace/old.txt
: > /workspace/old1.txt
: > /workspace/old2.txt

# Normalize generated mtimes before archiving. Dates use UTC and are fixed.
find /workspace /backup -exec touch -h -a -m -t 202305312359.58 {} +
touch -h -a -m -t 202205312359.59 /workspace/test/1dir
touch -h -a -m -t 202305302359.59 /workspace/recent.txt
touch -h -a -m -t 202305312359.59 /workspace/recent1.txt
touch -h -a -m -t 202302282359.59 /workspace/old.txt
touch -h -a -m -t 202304302359.59 /workspace/old1.txt
touch -h -a -m -t 202301312359.59 /workspace/old2.txt

## archive files: upstream referred to nonexistent /workspace/dir1/new.sh.
tar --sort=name --mtime='UTC 2023-05-31 23:59:58' --owner=0 --group=0 \
    --numeric-owner -cf - -C / workspace/dir2/foo.txt workspace/recent.txt \
    workspace/new.sh | gzip -n > /workspace/archive.tar.gz
touch -h -a -m -t 202305312359.58 /workspace/archive.tar.gz
find /workspace /backup -type d -exec touch -h -a -m -t 202305312359.58 {} +
touch -h -a -m -t 202205312359.59 /workspace/test/1dir
