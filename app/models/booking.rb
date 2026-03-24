class Booking < ApplicationRecord
  belongs_to :client
  belongs_to :freelancer

  enum :status, { pending: 0, confirmed: 1, completed: 2, cancelled: 3 }

  validates :start_date, presence: true
  validates :end_date, presence: true
  validates :total_amount, numericality: { greater_than_or_equal_to: 0 }
  validate :end_date_after_start_date

  private

  def end_date_after_start_date
    return if start_date.blank? || end_date.blank?

    if end_date <= start_date
      errors.add(:end_date, "must be after start date")
    end
  end
end
