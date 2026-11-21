import sys

if sys.argv[1:] == ['tray']:
    from .tray import main
    main()
elif sys.argv[1:]:
    sys.exit('usage: blackctl [tray]')
else:
    from .app import main
    sys.exit(main())
