import UIKit
import UserNotifications
import UserNotificationsUI

/// The long-press view of a PushPin push (category ARTICLE): the story on a card, and a red pin
/// that drops in and sticks. The Pin it button under it is handled by the app (AppDelegate).
final class NotificationViewController: UIViewController, UNNotificationContentExtension {
    private let card = UIView()
    private let titleLabel = UILabel()
    private let bodyLabel = UILabel()
    private let pin = UIImageView(image: UIImage(systemName: "pin.fill",
                                                 withConfiguration: UIImage.SymbolConfiguration(pointSize: 40)))

    override func viewDidLoad() {
        super.viewDidLoad()
        card.backgroundColor = .secondarySystemBackground
        card.layer.cornerRadius = 14
        titleLabel.font = UIFont(name: "Georgia-Bold", size: 21) ?? .boldSystemFont(ofSize: 21)
        titleLabel.numberOfLines = 0
        bodyLabel.font = .preferredFont(forTextStyle: .subheadline)
        bodyLabel.textColor = .secondaryLabel
        bodyLabel.numberOfLines = 4
        pin.tintColor = UIColor(red: 0.82, green: 0.01, blue: 0.11, alpha: 1)
        pin.layer.shadowColor = UIColor.black.cgColor
        pin.layer.shadowOpacity = 0.35
        pin.layer.shadowRadius = 2
        pin.layer.shadowOffset = CGSize(width: 3, height: 3)

        let stack = UIStackView(arrangedSubviews: [titleLabel, bodyLabel])
        stack.axis = .vertical
        stack.spacing = 8
        for v in [card, stack, pin] as [UIView] { v.translatesAutoresizingMaskIntoConstraints = false }
        view.addSubview(card)
        card.addSubview(stack)
        view.addSubview(pin)
        NSLayoutConstraint.activate([
            card.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 14),
            card.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -14),
            card.topAnchor.constraint(equalTo: view.topAnchor, constant: 30),
            card.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -14),
            stack.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 18),
            stack.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -40),
            stack.topAnchor.constraint(equalTo: card.topAnchor, constant: 18),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: card.bottomAnchor, constant: -18),
            pin.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -10),
            pin.topAnchor.constraint(equalTo: card.topAnchor, constant: -22),
        ])
    }

    func didReceive(_ notification: UNNotification) {
        titleLabel.text = notification.request.content.title
        bodyLabel.text = notification.request.content.body
        dropPin()
    }

    private func dropPin() {
        pin.transform = CGAffineTransform(translationX: 0, y: -260).rotated(by: -0.2)
        card.transform = .identity
        UIView.animate(withDuration: 0.55, delay: 0.1, usingSpringWithDamping: 0.55, initialSpringVelocity: 0.8) {
            self.pin.transform = CGAffineTransform(rotationAngle: 0.5)
        }
        UIView.animate(withDuration: 0.12, delay: 0.38, options: [.autoreverse]) {
            self.card.transform = CGAffineTransform(scaleX: 0.985, y: 0.985)   // the card gives as the pin goes in
        } completion: { _ in
            self.card.transform = .identity
        }
    }
}
