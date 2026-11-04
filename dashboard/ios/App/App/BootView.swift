import UIKit

/// The launch screen's twin, held over the page until the dashboard's first
/// real screen: the flat boot field with the white mark where the launch
/// storyboard draws it (139 pt of frame, the mark 127 pt wide, centred), so
/// nothing moves between the launch picture and the hold.
final class BootView: UIView {

    static let markFrame: CGFloat = 139

    private let mark = UIImageView(image: UIImage(named: "BootMark"))

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = UIColor(named: "BootField") ?? UIColor(red: 0.114, green: 0.310, blue: 0.502, alpha: 1)
        isUserInteractionEnabled = false
        mark.contentMode = .scaleAspectFit
        addSubview(mark)
    }

    required init?(coder: NSCoder) {
        fatalError("BootView is built in code")
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        let size = Self.markFrame
        mark.frame = CGRect(x: (bounds.width - size) / 2, y: (bounds.height - size) / 2, width: size, height: size)
    }
}

/// A short notice at the bottom of the window, the Android toast's twin:
/// it rides the window, so it outlives the controller that showed it.
enum Toast {

    private static var current: UIView?

    static func show(_ text: String, in window: UIWindow?) {
        DispatchQueue.main.async {
            guard let window = window ?? UIApplication.shared.connectedScenes
                .compactMap({ ($0 as? UIWindowScene)?.keyWindow }).first else { return }
            current?.removeFromSuperview()
            let label = UILabel()
            label.text = text
            label.numberOfLines = 3
            label.textAlignment = .center
            label.font = .preferredFont(forTextStyle: .footnote)
            label.textColor = .white
            let box = UIView()
            box.backgroundColor = UIColor(white: 0.1, alpha: 0.92)
            box.layer.cornerRadius = 10
            box.isUserInteractionEnabled = false
            box.translatesAutoresizingMaskIntoConstraints = false
            label.translatesAutoresizingMaskIntoConstraints = false
            box.addSubview(label)
            window.addSubview(box)
            NSLayoutConstraint.activate([
                label.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: 14),
                label.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -14),
                label.topAnchor.constraint(equalTo: box.topAnchor, constant: 10),
                label.bottomAnchor.constraint(equalTo: box.bottomAnchor, constant: -10),
                box.centerXAnchor.constraint(equalTo: window.centerXAnchor),
                box.bottomAnchor.constraint(equalTo: window.safeAreaLayoutGuide.bottomAnchor, constant: -24),
                box.widthAnchor.constraint(lessThanOrEqualTo: window.widthAnchor, constant: -48),
            ])
            box.alpha = 0
            current = box
            UIView.animate(withDuration: 0.2) { box.alpha = 1 }
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.2) {
                UIView.animate(withDuration: 0.25, animations: { box.alpha = 0 }, completion: { _ in
                    box.removeFromSuperview()
                    if current === box { current = nil }
                })
            }
        }
    }
}
