import sys

args = sys.argv[1:]
if args == ['tray']:
    from .tray import main
    main()
elif args[:1] == ['battery'] and args[1:] in ([], ['--json']):
    from .device import battery_text
    print(battery_text(as_json=args[1:] == ['--json']))
elif args:
    sys.exit('usage: blackctl [tray | battery [--json]]')
else:
    from .app import main
    sys.exit(main())
